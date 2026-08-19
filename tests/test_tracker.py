"""EventTracker: when a transmission starts, when it ends, and where.

Both edges are reported where they happened rather than where they were noticed,
which is the difference between a 2.00 s transmission logging as 2.00 s and as
2.42 s. Every duration and every airtime total in the database rests on this.
"""

import unittest

import support  # noqa: F401  — puts src/ on sys.path

import numpy as np


from survey_prototype import EventTracker

FRAME_S = 0.04          # -> min_frames 3, hang_frames 8 at the defaults
SAMPLES = 1000


def run_frames(tr, pattern, frame_samples=SAMPLES, can_start=None):
    """Drive the tracker one frame per entry. Returns (start_sample, end_sample)."""
    got_start = got_end = None
    for i, snr in enumerate(pattern):
        arr = np.array([float(snr)])
        started, ended = tr.update(arr, i * frame_samples,
                                   can_start=None if can_start is None
                                   else np.array([can_start[i]]))
        if len(started):
            got_start = tr.start_sample[0]
        if len(ended):
            got_end = tr.last_end_sample
    return got_start, got_end


class TestTiming(unittest.TestCase):

    def test_edges_are_reported_where_they_happened(self):
        tr = EventTracker(1, FRAME_S, min_duration=0.12, hang=0.30)
        # hot for frames 10..29 inclusive: 20 frames = 0.80 s
        pattern = [20.0 if 10 <= i < 30 else 0.0 for i in range(60)]
        start, end = run_frames(tr, pattern)
        self.assertEqual(start, 10 * SAMPLES, "start must be the first hot frame")
        self.assertEqual(end, 30 * SAMPLES, "end must be the first cold frame")
        duration = (end - start) / (SAMPLES / FRAME_S)
        self.assertAlmostEqual(duration, 0.80, places=9)

    def test_no_bias_at_any_duration(self):
        # The bias this corrects was a constant min_duration + hang, so it shows
        # up identically on a long transmission and dominates a short one.
        for n_frames in (5, 20, 100, 400):
            tr = EventTracker(1, FRAME_S, min_duration=0.12, hang=0.30)
            pattern = [20.0 if 10 <= i < 10 + n_frames else 0.0
                       for i in range(10 + n_frames + 40)]
            start, end = run_frames(tr, pattern)
            self.assertEqual((end - start) // SAMPLES, n_frames,
                             f"{n_frames}-frame signal mis-timed")

    def test_timing_survives_variable_frame_sizes(self):
        # readStream returns what it has, not a fixed count, so the tracker
        # cannot recover the edges by multiplying a nominal frame size. It keeps
        # the real sample offsets instead; this is the test for that.
        tr = EventTracker(1, FRAME_S, min_duration=0.12, hang=0.30)
        sizes = [700, 1300, 900, 1100, 1000, 800, 1200, 950, 1050, 1000]
        offsets, acc = [], 0
        for i in range(60):
            offsets.append(acc)
            acc += sizes[i % len(sizes)]
        got_start = got_end = None
        for i in range(60):
            snr = np.array([20.0 if 10 <= i < 30 else 0.0])
            started, ended = tr.update(snr, offsets[i])
            if len(started):
                got_start = tr.start_sample[0]
            if len(ended):
                got_end = tr.last_end_sample
        self.assertEqual(got_start, offsets[10])
        self.assertEqual(got_end, offsets[30])


class TestHysteresis(unittest.TestCase):

    def test_between_thresholds_holds_an_event_open(self):
        # on_db 10, off_db 6. A signal sitting at 8 dB is not strong enough to
        # start an event but must not end one either, or a transmission that
        # fades mid-sentence is logged as two.
        tr = EventTracker(1, FRAME_S, on_db=10.0, off_db=6.0)
        pattern = ([0.0] * 10 + [20.0] * 10 + [8.0] * 30 + [20.0] * 10
                   + [0.0] * 20)
        start, end = run_frames(tr, pattern)
        self.assertEqual(start, 10 * SAMPLES)
        self.assertEqual(end, 60 * SAMPLES, "the 8 dB stretch must not split it")

    def test_weak_signal_alone_never_starts_an_event(self):
        tr = EventTracker(1, FRAME_S, on_db=10.0, off_db=6.0)
        start, end = run_frames(tr, [8.0] * 60)
        self.assertIsNone(start, "8 dB is below on_db and must not trigger")

    def test_brief_spike_below_min_duration_is_ignored(self):
        tr = EventTracker(1, FRAME_S, min_duration=0.12)   # 3 frames
        start, _ = run_frames(tr, [0.0] * 10 + [20.0] * 2 + [0.0] * 30)
        self.assertIsNone(start, "2 frames is under min_duration")


class TestLocalMaxGating(unittest.TestCase):
    """can_start stops one transmission being logged on three channels."""

    def test_blocked_channel_cannot_open_an_event(self):
        tr = EventTracker(1, FRAME_S)
        pattern = [20.0] * 40
        start, _ = run_frames(tr, pattern, can_start=[False] * 40)
        self.assertIsNone(start, "can_start False must prevent the start")

    def test_gate_does_not_close_an_event_already_running(self):
        # The skirt channel of a neighbouring transmission may briefly become the
        # local maximum and then stop being one. That must not tear down an event
        # that is genuinely in progress.
        tr = EventTracker(1, FRAME_S)
        pattern = [20.0] * 60
        can_start = [True] * 10 + [False] * 50
        start, end = run_frames(tr, pattern, can_start=can_start)
        self.assertEqual(start, 0, "should have started while permitted")
        self.assertIsNone(end, "losing the gate must not end the event")

    def test_local_max_mask_picks_one_channel_of_three(self):
        # The mask as run() computes it, against a realistic 3-channel spread.
        snr = np.array([2.0, 9.0, 25.0, 11.0, 3.0])
        padded = np.concatenate(([-np.inf], snr, [-np.inf]))
        local_max = (snr >= padded[:-2]) & (snr >= padded[2:])
        self.assertTrue(local_max[2], "the peak must be allowed to start")
        self.assertFalse(local_max[1], "skirts must not")
        self.assertFalse(local_max[3])

    def test_local_max_mask_keeps_two_separated_signals(self):
        # Two real transmissions 12.5 kHz apart are two grid channels apart, and
        # both must survive the mask — otherwise the fix for double-logging would
        # start deleting genuine adjacent-channel activity.
        snr = np.array([2.0, 25.0, 3.0, 22.0, 2.0])
        padded = np.concatenate(([-np.inf], snr, [-np.inf]))
        local_max = (snr >= padded[:-2]) & (snr >= padded[2:])
        self.assertTrue(local_max[1])
        self.assertTrue(local_max[3])


if __name__ == "__main__":
    unittest.main()
