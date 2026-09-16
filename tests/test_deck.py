"""The deck tool, the status files and the dashboard's queries.

These run against temporary files and databases only. The deck tool edits the
survey's configuration and a profile an operator will read later, so the edits
must change exactly what they say and keep every comment.
"""

import os
import pathlib
import shutil
import sqlite3
import sys
import time
import unittest

import support

sys.path.insert(0, str(support.ROOT / "tools"))

import db as dbmod  # noqa: E402
import deck  # noqa: E402
import status  # noqa: E402


class EnvFile(support.TempDirCase):

    def test_updates_in_place_keeps_comments_and_appends(self):
        p = self.tmp / "rfsurvey.env"
        p.write_text("# header comment\nRFSURVEY_PROFILE=profiles/a.yaml\n# middle\nRFSURVEY_ENGINE=python\n")
        deck.write_env({"RFSURVEY_ENGINE": "rust", "RFSURVEY_DB": "data/x.sqlite"}, path=p)
        text = p.read_text()
        self.assertIn("# header comment", text)
        self.assertIn("# middle", text)
        self.assertLess(text.index("RFSURVEY_PROFILE"), text.index("RFSURVEY_ENGINE=rust"))
        env = deck.read_env(p)
        self.assertEqual(env["RFSURVEY_ENGINE"], "rust")
        self.assertEqual(env["RFSURVEY_DB"], "data/x.sqlite")
        self.assertEqual(env["RFSURVEY_PROFILE"], "profiles/a.yaml")

    def test_missing_keys_fall_back_to_unit_defaults(self):
        p = self.tmp / "empty.env"
        p.write_text("# nothing set\n")
        self.assertEqual(deck.read_env(p)["RFSURVEY_ENGINE"], deck.DEFAULTS["RFSURVEY_ENGINE"])


class ProfileGainEdit(support.TempDirCase):

    def test_changes_only_that_receiver_and_keeps_comments(self):
        import yaml
        copy = self.tmp / "festival.yaml"
        shutil.copy(support.PROFILE, copy)
        before = copy.read_text()
        other_before = yaml.safe_load(before)["receivers"]["uhf"]["gain"]
        self.assertTrue(deck.set_profile_gain(str(copy), "vhf", 39))
        after = copy.read_text()
        d = yaml.safe_load(after)
        self.assertEqual(d["receivers"]["vhf"]["gain"], 39)
        self.assertEqual(d["receivers"]["uhf"]["gain"], other_before)
        self.assertEqual(before.count("\n#"), after.count("\n#"))
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))

    def test_refuses_an_unknown_receiver(self):
        copy = self.tmp / "festival.yaml"
        shutil.copy(support.PROFILE, copy)
        before = copy.read_text()
        self.assertFalse(deck.set_profile_gain(str(copy), "hf", 30))
        self.assertEqual(copy.read_text(), before)


class StatusFiles(support.TempDirCase):

    def test_round_trip_and_liveness(self):
        self.addCleanup(setattr, status, "DIR", status.DIR)
        status.DIR = self.tmp
        status.write("uhf", {"state": "running", "fps": 152.6})
        got = status.read_all()["uhf"]
        self.assertTrue(got["alive"])
        self.assertAlmostEqual(got["fps"], 152.6)
        self.assertLess(got["age_s"], 5)
        status.write("uhf", {"state": "stopped"})
        self.assertFalse(status.read_all()["uhf"]["alive"])

    def test_tests_never_write_the_live_directory(self):
        self.assertNotEqual(os.environ.get("RFSURVEY_STATUS_DIR"), "/dev/shm")


class DashboardQueries(support.TempDirCase):

    def setUp(self):
        super().setUp()
        self.dbpath = self.path("survey.sqlite")
        dbmod.init_schema(self.dbpath)
        conn = dbmod.connect(self.dbpath)
        run = dbmod.start_run(conn, str(support.PROFILE), notes="test")
        now = time.time()
        self.rows = [
            # t offset s, rx, freq, duration, snr, ctcss, dcs, harmonic_of
            (-3500, "uhf", 462650000, 1.0, 30.0, 88.5, None, None),
            (-3000, "uhf", 462650000, 2.0, 32.0, 88.5, None, None),
            (-120, "uhf", 462650000, 0.5, 28.0, None, 23, None),
            (-100, "vhf", 151880000, 3.0, 25.0, None, None, None),
            (-90, "uhf", 463500000, 0.2, 15.0, None, None, None),
            (-80, "uhf", 461000000, 0.2, 10.0, None, None, 1),     # a harmonic
        ]
        for off, rx, f, dur, snr, ctcss, dcs, harm in self.rows:
            conn.execute(
                """INSERT INTO events (run_id, receiver_id, t_start, t_end, duration_s, freq_hz,
                                       snr_db, ctcss_hz, dcs_code, dcs_polarity, harmonic_of)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (run, rx, now + off, now + off + dur, dur, f, snr, ctcss, dcs, "N" if dcs else None, harm))
        conn.close()
        deck.DB_OVERRIDE = self.dbpath
        self.addCleanup(setattr, deck, "DB_OVERRIDE", None)
        import dashboard
        self.dash = dashboard

    def test_recent_events_newest_first_with_names_and_tones(self):
        ev = self.dash.api_events(10)["events"]
        self.assertEqual(len(ev), len(self.rows))
        self.assertEqual([e["t"] for e in ev], sorted((e["t"] for e in ev), reverse=True))
        frs = [e for e in ev if e["freq_hz"] == 462650000]
        self.assertEqual(frs[0]["channel"], "FRS 19/GMRS 19")
        self.assertEqual(frs[0]["tone"], "DCS 023N")
        self.assertTrue(any(e["harmonic"] for e in ev))

    def test_activity_counts_every_non_harmonic_event_once(self):
        a = self.dash.api_activity(60)
        self.assertEqual(len(a["bins"]), 60)
        total = sum(b["uhf"] + b["vhf"] for b in a["bins"])
        in_hour = [r for r in self.rows if r[0] >= -3600 + 60 and r[7] is None]
        self.assertGreaterEqual(total, len(in_hour) - 1)   # the oldest may sit on the hour's edge
        self.assertLessEqual(total, len([r for r in self.rows if r[7] is None]))
        self.assertEqual(sum(b["vhf"] for b in a["bins"]), 1)

    def test_busiest_excludes_harmonics_and_ranks_by_count(self):
        ch = self.dash.api_channels(60)["channels"]
        self.assertEqual(ch[0]["freq_hz"], 462650000)
        self.assertEqual(ch[0]["events"], 3)
        self.assertNotIn(461000000, [c["freq_hz"] for c in ch])
        self.assertIn("88.5 Hz", ch[0]["tones"])
        self.assertIn("DCS 023", ch[0]["tones"])


if __name__ == "__main__":
    unittest.main()
