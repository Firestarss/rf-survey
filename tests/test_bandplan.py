"""Band plan lookup.

The rule the whole labelling scheme rests on: narrowest match wins, so a specific
channel beats the segment it sits inside, and equally-specific matches are real
shared allocation rather than an ambiguity to resolve away.

These run against the seeded band plan, not a hand-built one — the seeding script
computes match windows from authorised bandwidth and neighbour spacing, and a
test that hand-wrote its own ranges would not exercise that.
"""

import contextlib
import io
import pathlib
import shutil
import sys
import tempfile
import unittest

from support import ROOT, SRC, TempDirCase  # noqa: F401

import bandplan
import db

sys.path.insert(0, str(ROOT / "tools"))
import seed_band_plan  # noqa: E402


class SeededCase(TempDirCase):
    """One seeded database per class, in a directory of its own.

    Seeded once because it is the same deterministic data every time and
    re-running it per test triples the module's runtime. In a temporary
    directory because SQLite leaves -wal and -shm alongside the file: writing it
    into tests/ left two of those in the working tree after every run, and
    deleting only the database itself never cleared them.
    """

    seeded = None

    @classmethod
    def setUpClass(cls):
        cls._seed_dir = tempfile.mkdtemp(prefix="rfsurvey-bandplan-")
        cls.addClassCleanup(shutil.rmtree, cls._seed_dir, ignore_errors=True)
        cls.seeded = str(pathlib.Path(cls._seed_dir) / "seeded.sqlite")
        db.init_schema(cls.seeded)
        with contextlib.redirect_stdout(io.StringIO()):
            seed_band_plan.main(cls.seeded)

    def setUp(self):
        super().setUp()
        self.conn = db.connect(self.seeded)
        self.addCleanup(self.conn.close)


class TestSpecificity(SeededCase):

    def test_a_channel_beats_the_segment_containing_it(self):
        row = bandplan.best(self.conn, 146_520_000)
        self.assertEqual(row["kind"], "channel")
        self.assertIn("calling", row["label"].lower())

    def test_a_frequency_with_no_channel_falls_through_to_the_segment(self):
        row = bandplan.best(self.conn, 146_470_000)
        self.assertEqual(row["kind"], "segment")
        self.assertEqual(row["service"], "ham2m")

    def test_an_unallocated_frequency_matches_nothing(self):
        self.assertIsNone(bandplan.best(self.conn, 463_112_500))
        self.assertEqual(bandplan.lookup(self.conn, 463_112_500), [])

    def test_matches_are_returned_narrowest_first(self):
        rows = bandplan.lookup(self.conn, 146_520_000)
        widths = [r["width_hz"] for r in rows]
        self.assertEqual(widths, sorted(widths))
        self.assertGreater(len(rows), 1, "should match both channel and segment")


class TestSharedAllocations(SeededCase):

    def test_462_675_is_genuinely_both_frs_and_gmrs(self):
        tied = bandplan.tied_best(self.conn, 462_675_000)
        services = {r["service"] for r in tied}
        self.assertEqual(services, {"frs", "gmrs"},
                         "this frequency really is both; the label must say so")

    def test_tied_best_returns_only_the_equally_specific_matches(self):
        tied = bandplan.tied_best(self.conn, 462_675_000)
        self.assertEqual(len({r["width_hz"] for r in tied}), 1)

    def test_best_breaks_the_tie_toward_the_unlicensed_service(self):
        # Stable and deliberate: FRS is the half anybody may transmit on. The
        # ordering must not depend on dict or hash iteration, which is how a
        # nondeterministic tie-break got into this project once already.
        first = [bandplan.best(self.conn, 462_675_000)["service"] for _ in range(5)]
        self.assertEqual(set(first), {"frs"})

    def test_a_measurement_slightly_off_channel_still_matches(self):
        # The detector reports a snapped grid frequency and real transmitters
        # drift; the match window is computed from bandwidth and neighbour
        # spacing precisely so this works.
        for offset in (-400, -100, 0, 100, 400):
            tied = bandplan.tied_best(self.conn, 462_675_000 + offset)
            self.assertTrue(tied, f"{offset:+d} Hz off channel matched nothing")
            self.assertEqual({r["service"] for r in tied}, {"frs", "gmrs"})


class TestChannelWindows(SeededCase):

    def test_no_two_channels_in_a_service_overlap(self):
        # seed_band_plan prints this count on every run and the handoff says it
        # must stay 0. FRS primary and interstitial channels interleave to
        # 12.5 kHz and 151.505 sits 7.5 kHz from 151.5125, so hand-written
        # tolerances would have overlapped silently.
        rows = self.conn.execute(
            """SELECT a.service, a.label, b.label AS other
               FROM band_plan a JOIN band_plan b
                 ON a.service = b.service AND a.id < b.id
                AND a.kind = 'channel' AND b.kind = 'channel'
                AND a.freq_lo_hz <= b.freq_hi_hz
                AND b.freq_lo_hz <= a.freq_hi_hz""").fetchall()
        self.assertEqual([tuple(r) for r in rows], [])

    def test_interstitial_neighbours_resolve_separately(self):
        # 12.5 kHz apart. If the windows were half the authorised bandwidth with
        # no neighbour check, these two would each claim the other's frequency.
        low = bandplan.tied_best(self.conn, 462_662_500)
        high = bandplan.tied_best(self.conn, 462_675_000)
        self.assertTrue(low and high)
        self.assertNotEqual({r["label"] for r in low}, {r["label"] for r in high})

    def test_every_channel_row_has_a_nominal_centre(self):
        missing = self.conn.execute(
            "SELECT label FROM band_plan WHERE kind='channel' "
            "AND freq_center_hz IS NULL").fetchall()
        self.assertEqual([r["label"] for r in missing], [],
                         "a channel with no nominal frequency cannot be programmed")

    def test_repeater_outputs_declare_their_offset(self):
        rows = self.conn.execute(
            "SELECT label, pair_offset_hz FROM band_plan "
            "WHERE is_repeater_out = 1").fetchall()
        self.assertTrue(rows, "the plan should contain repeater outputs")
        for row in rows:
            self.assertIsNotNone(row["pair_offset_hz"],
                                 f"{row['label']} is a repeater output with no offset")


class TestUnallocated(SeededCase):

    def test_reports_only_the_frequencies_nothing_covers(self):
        got = bandplan.unallocated(
            self.conn, [462_675_000, 463_112_500, 146_520_000, 400_000_000])
        self.assertEqual(sorted(got), [400_000_000, 463_112_500])


if __name__ == "__main__":
    unittest.main()
