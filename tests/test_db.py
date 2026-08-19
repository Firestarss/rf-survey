"""db.py: the layer everything writes through.

Its job is to make wrong writes impossible rather than merely unlikely, so most
of these check that bad input is refused rather than that good input works.
"""

import sqlite3
import unittest

from support import PROFILE, SRC, TempDirCase  # noqa: F401

import db
import migrate


class DbCase(TempDirCase):

    def setUp(self):
        super().setUp()
        self.dbpath = self.path("t.sqlite")
        db.init_schema(self.dbpath)
        self.conn = db.connect(self.dbpath)
        self.run_id = db.start_run(self.conn, str(PROFILE), notes="unit test")
        self.conn.commit()


class TestConnection(DbCase):

    def test_foreign_keys_are_enforced(self):
        # Per-connection and off by default in SQLite. Without it, a decode can
        # reference an event that does not exist and nothing complains.
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO events (run_id, receiver_id, t_start, "
                              "freq_hz) VALUES (999,'uhf',1.0,1)")

    def test_rows_are_addressable_by_name(self):
        row = self.conn.execute("SELECT * FROM runs").fetchone()
        self.assertEqual(row["id"], self.run_id)

    def test_readonly_connections_cannot_write(self):
        ro = db.connect(self.dbpath, readonly=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                       "VALUES (1.0,'x','y')")

    def test_init_schema_is_idempotent(self):
        before = migrate.current_version(self.conn)
        db.init_schema(self.dbpath)
        self.assertEqual(migrate.current_version(db.connect(self.dbpath)), before)


class TestLogEvent(DbCase):

    def log(self, **kw):
        fields = {"t_start": 1000.0, "freq_hz": 462_675_000}
        fields.update(kw)
        return db.log_event(self.conn, self.run_id, "uhf", **fields)

    def test_unknown_columns_are_refused_loudly(self):
        # A typo'd field name must not be silently dropped: the row would look
        # complete and be missing the measurement it was written for.
        with self.assertRaises(ValueError) as cm:
            self.log(ctcss_freq=141.3)
        self.assertIn("ctcss_freq", str(cm.exception))

    def test_every_column_the_detector_writes_is_accepted(self):
        # Migration 5 and 6 added columns and this allow-list was not updated for
        # all of them; center_hz went missing from run_receivers the same way.
        row = self.log(t_end=1002.0, duration_s=2.0, freq_raw_hz=462_674_800,
                       snr_db=30.0, peak_dbfs=-58.0, noise_dbfs=-88.0,
                       modulation="fm", deviation_hz=2400.0, ctcss_hz=141.3,
                       ctcss_dev_hz=700.0, confidence=0.99, tone_state="ctcss",
                       overload=0, analyzed_s=0.9, audio_path="/tmp/a.wav")
        stored = self.conn.execute("SELECT * FROM events WHERE id=?", (row,)).fetchone()
        self.assertEqual(stored["deviation_hz"], 2400.0)
        self.assertEqual(stored["analyzed_s"], 0.9)
        self.assertEqual(stored["ctcss_dev_hz"], 700.0)

    def test_invalid_tone_state_is_refused(self):
        with self.assertRaises(ValueError):
            self.log(tone_state="probably")

    def test_none_is_omitted_rather_than_written(self):
        # "Unknown fields must be omitted, not zeroed" — a 0 Hz deviation and an
        # unmeasured one are different facts and the tier ladder turns on it.
        row = self.log(deviation_hz=None)
        stored = self.conn.execute("SELECT * FROM events WHERE id=?", (row,)).fetchone()
        self.assertIsNone(stored["deviation_hz"])

    def test_schema_checks_still_apply(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.log(confidence=1.5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.log(t_end=999.0)          # before t_start
        with self.assertRaises(sqlite3.IntegrityError):
            self.log(ctcss_hz=141.3, dcs_code=23)   # cannot be both


class TestRunLifecycle(DbCase):

    def test_profile_is_snapshotted_verbatim(self):
        # The file on disk drifts; the run has to keep saying what it used.
        row = self.conn.execute("SELECT * FROM runs WHERE id=?",
                                (self.run_id,)).fetchone()
        self.assertEqual(row["profile_yaml"], PROFILE.read_text())
        self.assertEqual(row["profile_name"], "festival")

    def test_receiver_records_everything_it_was_given(self):
        db.register_receiver(self.conn, self.run_id, "uhf", serial="ABC123",
                             sample_rate_hz=10_000_000, gain_db=12.0,
                             ppm_error=0.5, attenuator_db=20.0,
                             antenna="discone", center_hz=466_000_000)
        row = self.conn.execute("SELECT * FROM run_receivers").fetchone()
        self.assertEqual(row["serial"], "ABC123")
        self.assertEqual(row["center_hz"], 466_000_000)
        self.assertEqual(row["attenuator_db"], 20.0)
        self.assertEqual(row["antenna"], "discone")

    def test_one_receiver_row_per_run(self):
        db.register_receiver(self.conn, self.run_id, "uhf", "A", 10_000_000)
        with self.assertRaises(sqlite3.IntegrityError):
            db.register_receiver(self.conn, self.run_id, "uhf", "B", 10_000_000)

    def test_end_run_stamps_the_finish(self):
        db.end_run(self.conn, self.run_id)
        row = self.conn.execute("SELECT ended_at FROM runs").fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_windows_open_and_close(self):
        wid = db.open_window(self.conn, self.run_id, "vhf", 146_000_000,
                             10_000_000, "2m ham")
        row = self.conn.execute("SELECT * FROM coverage_windows").fetchone()
        self.assertEqual(row["label"], "2m ham")
        self.assertIsNone(row["t_end"], "a window is open until it is closed")
        db.close_window(self.conn, wid)
        row = self.conn.execute("SELECT * FROM coverage_windows").fetchone()
        self.assertIsNotNone(row["t_end"])
        self.assertGreaterEqual(row["t_end"], row["t_start"])

    def test_deleting_a_run_takes_its_events_with_it(self):
        # make_fixtures --wipe relies on this cascade.
        db.log_event(self.conn, self.run_id, "uhf", t_start=1.0, freq_hz=1)
        self.conn.execute("DELETE FROM runs WHERE id=?", (self.run_id,))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events")
                         .fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
