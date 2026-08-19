"""NoiseFloor: the estimate of what "quiet" means on each channel.

Everything the detector does is a comparison against this, so a floor that drifts
does not produce a wrong number — it produces a missing transmission or an
invented one.

The failure that motivated most of this module: the estimate is a low percentile
over a fixed history, so a carrier that stays up long enough to fill the rest of
that history drags the floor up to meet itself. The SNR then collapses and the
detector calls the transmission over while it is still going. At the defaults
that happens after 1.26 s, so a 30-second ham QSO logged as 1.27 seconds.
"""

import unittest

import support  # noqa: F401  — puts src/ on sys.path

import numpy as np


from survey_prototype import FLOOR_FRAMES, FLOOR_PCTILE, NoiseFloor

QUIET = -90.0
LOUD = -60.0


def drive(floor, frames, power, active_from=None, n_channels=4):
    """Feed `frames` frames of `power`, optionally freezing channel 0."""
    active = None
    last = None
    for i in range(frames):
        last = floor.update(power, active=active)
        if last is not None and active_from is not None and i >= active_from:
            active = np.array([True] + [False] * (n_channels - 1))
    return last


class TestWarmUp(unittest.TestCase):

    def test_reports_nothing_until_it_has_history(self):
        # 20 frames of history before it will commit to a number. Until then the
        # detector cannot fire at all, which is roughly a quarter of a second.
        floor = NoiseFloor(4)
        quiet = np.full(4, QUIET)
        for i in range(19):
            self.assertIsNone(floor.update(quiet), f"committed after {i+1} frames")
        self.assertIsNotNone(floor.update(quiet), "should be warm at 20 frames")

    def test_settles_on_the_quiet_level(self):
        floor = NoiseFloor(4)
        got = drive(floor, FLOOR_FRAMES + 20, np.full(4, QUIET))
        np.testing.assert_allclose(got, QUIET, atol=0.01)

    def test_warm_up_is_about_one_and_a_half_seconds(self):
        # 120 frames at ~13 ms. Worth pinning: a simulation or a test that starts
        # transmitting sooner than this sees nothing and looks like a bug in the
        # detector rather than in its own timing.
        self.assertLess(FLOOR_FRAMES * 0.0131, 2.0)
        self.assertGreater(FLOOR_FRAMES * 0.0131, 1.0)


class TestSustainedCarrier(unittest.TestCase):

    def _run(self, freeze):
        floor = NoiseFloor(4)
        quiet = np.full(4, QUIET)
        for _ in range(FLOOR_FRAMES + 10):        # warm up on silence
            floor.update(quiet)
        loud = np.array([LOUD, QUIET, QUIET, QUIET])
        active = np.array([True, False, False, False]) if freeze else None
        last = None
        for _ in range(FLOOR_FRAMES * 2):         # carrier stays up throughout
            last = floor.update(loud, active=active)
        return last

    def test_an_active_channel_does_not_raise_its_own_floor(self):
        got = self._run(freeze=True)
        self.assertAlmostEqual(float(got[0]), QUIET, delta=0.5,
                               msg="the carrier joined its own noise estimate")

    def test_without_the_freeze_the_floor_chases_the_carrier(self):
        # Establishes that the test above is testing something. If this ever
        # stops rising, the freeze is no longer what is protecting the estimate
        # and the other test has become vacuous.
        got = self._run(freeze=False)
        self.assertGreater(float(got[0]), QUIET + 20,
                           "expected the unfrozen floor to rise toward the carrier")

    def test_neighbouring_channels_are_unaffected(self):
        got = self._run(freeze=True)
        np.testing.assert_allclose(got[1:], QUIET, atol=0.5)

    def test_snr_stays_above_threshold_for_the_whole_transmission(self):
        # The property that actually matters downstream.
        floor = NoiseFloor(4)
        quiet = np.full(4, QUIET)
        for _ in range(FLOOR_FRAMES + 10):
            floor.update(quiet)
        loud = np.array([LOUD, QUIET, QUIET, QUIET])
        active = np.array([True, False, False, False])
        worst = 999.0
        for _ in range(FLOOR_FRAMES * 3):
            f = floor.update(loud, active=active)
            worst = min(worst, float((loud - f)[0]))
        self.assertGreater(worst, 6.0, "SNR fell below off_db mid-transmission")


class TestQuietLevelTracking(unittest.TestCase):

    def test_follows_a_rising_noise_floor(self):
        # The band really does get noisier as a site fills up; the estimate has
        # to follow that, which is why it is a rolling window and not a constant.
        floor = NoiseFloor(4)
        drive(floor, FLOOR_FRAMES + 10, np.full(4, QUIET))
        got = drive(floor, FLOOR_FRAMES * 2, np.full(4, QUIET + 10))
        np.testing.assert_allclose(got, QUIET + 10, atol=0.5)

    def test_a_brief_burst_does_not_move_it(self):
        floor = NoiseFloor(4)
        quiet = np.full(4, QUIET)
        drive(floor, FLOOR_FRAMES + 10, quiet)
        loud = np.array([LOUD, QUIET, QUIET, QUIET])
        for _ in range(int(FLOOR_FRAMES * (FLOOR_PCTILE / 100.0) * 0.5)):
            got = floor.update(loud)          # deliberately not frozen
        np.testing.assert_allclose(got, QUIET, atol=0.5,
                                   err_msg="a short burst should not move a "
                                           "low-percentile estimate")


if __name__ == "__main__":
    unittest.main()
