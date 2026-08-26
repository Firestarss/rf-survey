"""Receiver products: odd harmonics of a strong signal's baseband offset.

Measured on hardware 2026-08-27. A handheld reading 64.8 dB SNR produced 124
events across a 2.49 MHz window; what survived at sane gain was a comb at odd
multiples of the carrier's offset from the tuned centre — exactly -3, +5, -7,
+9, -11. The comb spacing changed from 150 kHz to 450 kHz when the transmitter
moved from 37.5 kHz below centre to 112.5 kHz above it, which is what identified
the mechanism.

These matter more than ordinary false positives because they are derived from a
real transmission and inherit its properties: the same duration to within 14 ms
and the same CTCSS tone at capture ratio 1.0. Nothing else the deck records
distinguishes them from traffic on a channel nobody keyed.
"""

import sqlite3
import unittest

import support  # noqa: F401

import db as dbmod
import enrich
import migrate


class HarmonicSchema(unittest.TestCase):
    """Migration 10, and that the columns survive a real upgrade."""

    def test_upgrade_from_v9_keeps_rows(self):
        conn = support.build_at_version(":memory:", 9)
        conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1.0, 'test', 'x')")
        run = conn.execute("SELECT id FROM runs").fetchone()[0]
        for f in (462562500, 462262500):
            dbmod.log_event(conn, run, "uhf", t_start=1.0, freq_hz=f,
                            modulation="fm", tone_state="unknown")
        migrate.apply(conn, 10)
        self.assertEqual(migrate.current_version(conn), 10)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        self.assertIn("harmonic_of", cols)
        self.assertIn("harmonic_n", cols)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_parent_deletion_does_not_orphan(self):
        """ON DELETE SET NULL: losing the parent must not delete the child.

        The child is still a real observation — the deck genuinely saw energy
        there. What it loses is the explanation.
        """
        conn = support.build_at_version(":memory:", 10)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1.0, 'test', 'x')")
        run = conn.execute("SELECT id FROM runs").fetchone()[0]
        parent = dbmod.log_event(conn, run, "uhf", t_start=1.0,
                                 freq_hz=462712500, modulation="fm",
                                 tone_state="unknown")
        child = dbmod.log_event(conn, run, "uhf", t_start=1.0,
                                freq_hz=462262500, modulation="fm",
                                tone_state="unknown")
        conn.execute("UPDATE events SET harmonic_of=?, harmonic_n=-3 WHERE id=?",
                     (parent, child))
        conn.execute("DELETE FROM events WHERE id = ?", (parent,))
        row = conn.execute("SELECT harmonic_of, harmonic_n FROM events "
                           "WHERE id = ?", (child,)).fetchone()
        self.assertIsNotNone(row, "child was deleted with its parent")
        self.assertIsNone(row["harmonic_of"])


class EnricherExcludesHarmonics(support.TempDirCase):
    """They are observations, not traffic, and must not become channels."""

    def setUp(self):
        super().setUp()
        self.path = self.path("h.sqlite")
        dbmod.init_schema(self.path)
        self.conn = dbmod.connect(self.path)
        self.conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml) "
                     "VALUES (1.0, 'test', 'x')")
        self.run = self.conn.execute("SELECT id FROM runs").fetchone()[0]

    def add(self, freq, harmonic_of=None, n=None):
        row = dbmod.log_event(self.conn, self.run, "uhf", t_start=1.0,
                              freq_hz=freq, modulation="fm",
                              tone_state="unknown")
        self.conn.execute("UPDATE events SET t_end=3.0, duration_s=2.0, "
                          "snr_db=30.0, harmonic_of=?, harmonic_n=? WHERE id=?",
                          (harmonic_of, n, row))
        return row

    def test_harmonic_never_becomes_a_channel(self):
        parent = self.add(462712500)
        self.add(462262500, harmonic_of=parent, n=-3)
        self.add(463162500, harmonic_of=parent, n=5)
        enrich.tag(self.conn)
        enrich.rollup(self.conn)
        freqs = {r["freq_hz"] for r in
                 self.conn.execute("SELECT freq_hz FROM channels")}
        self.assertIn(462712500, freqs, "the real transmission is missing")
        self.assertNotIn(462262500, freqs, "a harmonic became a channel")
        self.assertNotIn(463162500, freqs, "a harmonic became a channel")

    def test_harmonic_rows_are_still_there_to_audit(self):
        """Excluded from derived tables, not deleted. The discriminator has not
        been checked against a season of real data, and 462.2625 is somebody's
        licensed frequency."""
        parent = self.add(462712500)
        self.add(462262500, harmonic_of=parent, n=-3)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE harmonic_of IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_harmonic_does_not_inflate_the_parents_airtime(self):
        parent = self.add(462712500)
        for f, n in ((462262500, -3), (463162500, 5), (461812500, -7)):
            self.add(f, harmonic_of=parent, n=n)
        enrich.tag(self.conn)
        enrich.rollup(self.conn)
        row = self.conn.execute(
            "SELECT event_count, total_airtime_s FROM channels "
            "WHERE freq_hz = ?", (462712500,)).fetchone()
        self.assertEqual(row["event_count"], 1)
        self.assertAlmostEqual(row["total_airtime_s"], 2.0, places=3)


if __name__ == "__main__":
    unittest.main()
