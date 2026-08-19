"""The capture loop, driven end to end against a synthetic radio.

Everything else in this suite tests a piece. This runs the real `run()` — detect,
analyse, two-phase write, retune, teardown — and asserts on the database that
comes out. It is the only test that would have caught any of: deviation reading
the noise floor on every event, a 4-second transmission logging as 1.27 s, one
keyup being logged as three, or no transmission ever having its tone identified.
Each of those looked fine in isolation and only appeared once the parts ran
together.

Slow: it synthesises and processes real sample streams. Set RFSURVEY_SKIP_SLOW=1
to leave it out.
"""

import argparse
import contextlib
import io
import os
import pathlib
import shutil
import tempfile
import unittest

from support import PROFILE, TempDirCase

import db
import dcs
import simradio
import survey_prototype as proto

SKIP = os.environ.get("RFSURVEY_SKIP_SLOW") == "1"
RATE = 2.4e6
UHF = 466_000_000
DCS_CODE = sorted(dcs.STANDARD_CODES)[0]

# Placed at least two grid channels apart so the local-maximum rule keeps them
# distinct, and staggered so each duration is attributable to one transmission.
# t0 clears the noise-floor warm-up, which is about 1.6 s at any sample rate.
PARKED_SCENARIO = [
    ("long",  UHF + 12_500,  2.0, 2.5, dict(ctcss_hz=141.3, deviation_hz=4900)),
    ("copy",  UHF + 50_000,  4.8, 0.8, dict(ctcss_hz=88.5, deviation_hz=2400)),
    ("short", UHF - 25_000,  5.9, 0.3, dict(deviation_hz=2400)),
    ("dcs",   UHF + 100_000, 6.5, 1.6, dict(dcs_code=DCS_CODE, deviation_hz=2400)),
]


def scenario():
    return [simradio.Transmission(freq, t0, dur, snr_db=34, label=name, **kw)
            for name, freq, t0, dur, kw in PARKED_SCENARIO]


def args_for(dbpath, **kw):
    base = dict(profile=str(PROFILE), receiver_id="uhf", db=dbpath, notes="test",
                rate=RATE, gain=None, ppm=None, on_db=None, off_db=None,
                serial=None, freq=float(UHF), dwell_seconds=None, simulate=9.0,
                driver="airspy", capture_dir=None, capture_mb=2000.0,
                capture_iq=False, stats=False)
    base.update(kw)
    return argparse.Namespace(**base)


def drive(args, scenario_fn):
    """Run the capture loop, keeping its console output for failure messages."""
    original = simradio.festival_scenario
    simradio.festival_scenario = scenario_fn
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = proto.run(args)
    finally:
        simradio.festival_scenario = original
    return rc, out.getvalue()


class CaptureRunCase(TempDirCase):
    """One capture run per class, shared by every test that reads its database.

    The run is the expensive part — it synthesises and processes real sample
    streams — and it is deterministic, so driving it once per test method meant
    24 identical runs and about ninety seconds. Each test still gets its own
    connection, and anything that writes works on a copy.
    """

    @classmethod
    def scenario_fn(cls):
        """The transmissions this class's run should see."""
        raise NotImplementedError

    @classmethod
    def run_args(cls, workdir):
        """Anything beyond the defaults in args_for(), given a scratch dir."""
        return {}

    @classmethod
    def setUpClass(cls):
        cls._dir = pathlib.Path(tempfile.mkdtemp(prefix="rfsurvey-e2e-"))
        cls.addClassCleanup(shutil.rmtree, cls._dir, ignore_errors=True)
        cls.dbpath = str(cls._dir / "e2e.sqlite")
        cls.rc, cls.log = drive(
            args_for(cls.dbpath, **cls.run_args(cls._dir)), cls.scenario_fn())

    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.dbpath)
        self.addCleanup(self.conn.close)

    def scratch_copy(self):
        """The run's database, copied, for a test that needs to write to it."""
        path = self.path("scratch.sqlite")
        shutil.copy(self.dbpath, path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn


@unittest.skipIf(SKIP, "RFSURVEY_SKIP_SLOW=1")
class TestParkedRun(CaptureRunCase):

    @classmethod
    def scenario_fn(cls):
        return scenario

    @classmethod
    def run_args(cls, workdir):
        return dict(capture_dir=str(workdir / "caps"))

    def setUp(self):
        super().setUp()
        self.events = self.conn.execute(
            "SELECT * FROM events ORDER BY t_start").fetchall()

    def near(self, freq_hz):
        """The one event on the channel closest to `freq_hz`."""
        matches = [e for e in self.events if abs(e["freq_hz"] - freq_hz) < 6250]
        self.assertEqual(len(matches), 1,
                         f"expected exactly one event near {freq_hz/1e6:.4f} MHz, "
                         f"got {[e['freq_hz'] for e in matches]}\n{self.log}")
        return matches[0]

    def test_clean_exit(self):
        self.assertEqual(self.rc, 0)

    def test_one_event_per_transmission(self):
        # An FM signal occupies about 11 kHz on a 6.25 kHz grid, so without the
        # local-maximum rule each keyup is logged on three channels — and because
        # FRS channels interleave at 12.5 kHz, the skirts land on real channel
        # numbers and invent traffic nobody generated.
        self.assertEqual(len(self.events), len(PARKED_SCENARIO),
                         f"expected {len(PARKED_SCENARIO)} events, "
                         f"got {len(self.events)}\n{self.log}")

    def test_frequencies_land_on_the_right_channels(self):
        for _, freq, _, _, _ in PARKED_SCENARIO:
            row = self.near(freq)
            self.assertLess(abs(row["freq_hz"] - freq), proto.CHANNEL_HZ)

    def test_durations_match_the_transmissions(self):
        # The noise floor absorbs a carrier that stays up: before that was fixed,
        # everything longer than 1.27 s logged as 1.27 s.
        for name, freq, _, dur, _ in PARKED_SCENARIO:
            row = self.near(freq)
            self.assertIsNotNone(row["duration_s"], f"{name} never closed")
            self.assertAlmostEqual(
                row["duration_s"], dur, delta=0.2,
                msg=f"{name}: {dur}s transmission logged as "
                    f"{row['duration_s']}s\n{self.log}")

    def test_deviation_is_measured_not_the_noise_floor(self):
        for name, freq, _, _, kw in PARKED_SCENARIO:
            row = self.near(freq)
            want = kw["deviation_hz"]
            self.assertIsNotNone(row["deviation_hz"], f"{name} has no deviation")
            self.assertLess(row["deviation_hz"], want * 1.5,
                            f"{name} reads {row['deviation_hz']:.0f} Hz for a "
                            f"{want} Hz signal — the noise figure is ~11700")
            self.assertGreater(row["deviation_hz"], want * 0.7)

    def test_wide_and_narrow_are_distinguishable(self):
        import enrich
        thresh = (enrich.DEVIATION_LIMIT_HZ[12_500]
                  + enrich.DEV_EVIDENCE_MARGIN_HZ)
        self.assertGreater(self.near(UHF + 12_500)["deviation_hz"], thresh)
        self.assertLess(self.near(UHF + 50_000)["deviation_hz"], thresh)

    def test_ctcss_is_identified_on_a_long_transmission(self):
        row = self.near(UHF + 12_500)
        self.assertEqual(row["tone_state"], "ctcss")
        self.assertEqual(row["ctcss_hz"], 141.3)

    def test_ctcss_is_identified_on_a_short_one(self):
        # 0.8 s: never reaches the analysis trigger, so this only works because
        # the event is analysed on close with whatever dwell it had.
        row = self.near(UHF + 50_000)
        self.assertEqual(row["tone_state"], "ctcss")
        self.assertEqual(row["ctcss_hz"], 88.5)

    def test_dcs_is_decoded(self):
        row = self.near(UHF + 100_000)
        self.assertEqual(row["tone_state"], "dcs")
        self.assertEqual(row["dcs_code"], int(DCS_CODE))
        self.assertEqual(row["dcs_polarity"], "N")
        self.assertLessEqual(row["dcs_errors"], 3)

    def test_a_keyup_too_short_to_analyse_still_reaches_tier_one(self):
        row = self.near(UHF - 25_000)
        self.assertIsNotNone(row["deviation_hz"], "deviation needs almost no dwell")
        self.assertEqual(row["tone_state"], "unknown",
                         "not checked is a different claim from no tone")
        self.assertIsNone(row["ctcss_hz"])

    def test_analyzed_s_reports_signal_not_window(self):
        for name, freq, _, dur, _ in PARKED_SCENARIO:
            row = self.near(freq)
            self.assertIsNotNone(row["analyzed_s"])
            self.assertLessEqual(row["analyzed_s"], dur + 0.15,
                                 f"{name} claims more signal than was transmitted")

    def test_frequency_error_is_recorded_for_phase_one(self):
        row = self.near(UHF + 12_500)
        self.assertIsNotNone(row["freq_raw_hz"])
        self.assertLess(abs(row["freq_raw_hz"] - row["freq_hz"]), 2000)

    def test_the_run_is_recorded_completely(self):
        run = self.conn.execute("SELECT * FROM runs").fetchone()
        self.assertIsNotNone(run["ended_at"], "run was never closed")
        self.assertIn("receivers:", run["profile_yaml"])
        rx = self.conn.execute("SELECT * FROM run_receivers").fetchone()
        self.assertEqual(rx["receiver_id"], "uhf")
        self.assertEqual(rx["serial"], "SIM-UHF")
        self.assertEqual(rx["center_hz"], UHF,
                         "migration 5 added this column and nothing wrote it")
        self.assertEqual(rx["sample_rate_hz"], int(RATE))

    def test_coverage_window_is_opened_and_closed(self):
        rows = self.conn.execute("SELECT * FROM coverage_windows").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["center_hz"], UHF)
        self.assertIsNotNone(rows[0]["t_end"], "window left open")

    def test_timestamps_are_self_consistent(self):
        # t_start, t_end and duration_s are three views of the same fact and the
        # schema enforces only the weakest relation between them. Deriving them
        # from two different clocks — wall time per frame, offsets from the
        # sample counter — produced events stamped as ending before they began.
        for row in self.events:
            self.assertGreaterEqual(row["t_end"], row["t_start"])
            self.assertAlmostEqual(row["t_end"] - row["t_start"],
                                   row["duration_s"], delta=0.05,
                                   msg="duration disagrees with the timestamps")

    def test_events_reference_their_coverage_window(self):
        for row in self.events:
            self.assertIsNotNone(row["window_id"])

    def test_captures_are_written_and_linked(self):
        for row in self.events:
            self.assertIsNotNone(row["audio_path"],
                                 f"no recording for {row['freq_hz']}")
            self.assertTrue(os.path.exists(row["audio_path"]))
            self.assertIsNone(row["iq_path"], "IQ retention was not requested")

    def test_database_is_internally_consistent(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM events WHERE run_id != 1")
            .fetchone()[0], 0)

    def test_enricher_runs_on_what_the_deck_produced(self):
        # The deck's output has to be something the rest of the pipeline can
        # consume; the fixtures prove the enricher works on synthetic rows, not
        # on rows this code path actually writes.
        import enrich
        conn = self.scratch_copy()      # the only test here that writes
        with contextlib.redirect_stdout(io.StringIO()):
            enrich.tag(conn)
            n = enrich.rollup(conn)
            enrich.pair(conn)
            tiers = enrich.score(conn, {"gmrs", "frs"})
        self.assertEqual(n, len(PARKED_SCENARIO))
        self.assertTrue(all(t >= 1 for t in tiers.values()),
                        "every analysed channel should clear tier 0")


@unittest.skipIf(SKIP, "RFSURVEY_SKIP_SLOW=1")
class TestRotation(CaptureRunCase):
    """A rotating receiver must visit every window and say what it heard where."""

    WINDOWS = {"70cm ham": 446_000_000, "2m ham": 146_000_000,
               "MURS + VHF business": 154_950_000}

    @classmethod
    def scenario_fn(cls):
        def rotating_scenario():
            return [simradio.Transmission(center + 12_500, 2.0, 1.2,
                                          deviation_hz=2400, snr_db=34,
                                          label=name)
                    for name, center in cls.WINDOWS.items()]
        return rotating_scenario

    # The dwell is deliberately far longer than the scenario. dwell_seconds is
    # wall-clock, which is what a real deck wants — "spend three minutes on each
    # band" — but the simulator produces sample time, and a Pi does not
    # synthesise 2.4 MSPS in real time. A wall-clock dwell short enough to be
    # quick would end each window part-way through its transmission, and how far
    # through would depend on how busy the machine was. Letting the scenario run
    # out is what advances the window here, so the test measures the retune
    # logic rather than the speed of the host.
    @classmethod
    def run_args(cls, workdir):
        return dict(receiver_id="vhf", freq=None, dwell_seconds=3600.0,
                    simulate=4.0)

    def test_every_window_was_visited(self):
        rows = self.conn.execute(
            "SELECT center_hz, t_end FROM coverage_windows").fetchall()
        self.assertEqual({r["center_hz"] for r in rows},
                         set(self.WINDOWS.values()),
                         f"not every window was tuned to\n{self.log}")
        for row in rows:
            self.assertIsNotNone(row["t_end"], "window left open")

    def test_one_event_per_window(self):
        rows = self.conn.execute("SELECT freq_hz FROM events").fetchall()
        self.assertEqual(len(rows), len(self.WINDOWS),
                         f"expected one event per window\n{self.log}")

    def test_events_are_attributed_to_the_window_that_heard_them(self):
        # The point of coverage_windows: a band with no events because nothing
        # tuned to it looks identical in `events` to a band that was quiet.
        for row in self.conn.execute("SELECT * FROM v_coverage"):
            self.assertEqual(row["events"], 1,
                             f"{row['window']} should have exactly one event")

    def test_every_event_names_the_window_that_heard_it(self):
        rows = self.conn.execute(
            """SELECT e.freq_hz, w.center_hz, w.label
               FROM events e LEFT JOIN coverage_windows w ON w.id = e.window_id""",
        ).fetchall()
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(row["center_hz"],
                                 "event does not reference a coverage window")
            self.assertLess(abs(row["freq_hz"] - row["center_hz"]), RATE / 2,
                            f"{row['freq_hz']} logged against {row['label']}, "
                            f"which was never tuned to it")

    def test_each_window_recorded_a_distinct_event(self):
        window_ids = [r["window_id"] for r in
                      self.conn.execute("SELECT window_id FROM events")]
        self.assertEqual(len(set(window_ids)), len(self.WINDOWS),
                         "events did not spread across the windows")


if __name__ == "__main__":
    unittest.main()
