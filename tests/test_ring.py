"""The IQ ring buffer.

Addressed by absolute sample index, written continuously and read from behind, so
every bug in it is an off-by-one that silently hands the analyser the wrong slice
of spectrum-time. Nothing downstream can detect that: the samples are valid, just
not the ones asked for.
"""

import unittest

import numpy as np

from support import SRC  # noqa: F401

from survey_prototype import Ring


def ramp(start, n):
    """Samples whose value encodes their own absolute index."""
    return (np.arange(start, start + n) + 1j * 0).astype(np.complex64)


class TestRing(unittest.TestCase):

    def test_read_back_what_was_written(self):
        r = Ring(100)
        r.push(ramp(0, 40))
        got = r.get(10, 20)
        np.testing.assert_array_equal(got, ramp(10, 20))

    def test_read_across_the_wrap(self):
        # The case that matters: a read that starts before the wrap point and
        # ends after it. Capacity 100, so index 100 lands back at buffer slot 0.
        r = Ring(100)
        for start in range(0, 180, 30):
            r.push(ramp(start, 30))
        got = r.get(95, 20)
        np.testing.assert_array_equal(got, ramp(95, 20))

    def test_read_exactly_at_the_boundary(self):
        r = Ring(64)
        r.push(ramp(0, 64))
        np.testing.assert_array_equal(r.get(0, 64), ramp(0, 64))
        r.push(ramp(64, 1))
        self.assertIsNone(r.get(0, 64), "oldest sample has been overwritten")
        np.testing.assert_array_equal(r.get(1, 64), ramp(1, 64))

    def test_aged_out_returns_none_rather_than_stale_data(self):
        r = Ring(50)
        r.push(ramp(0, 200))
        self.assertIsNone(r.get(0, 10), "long-overwritten data must not be served")
        self.assertIsNone(r.get(149, 10), "just-overwritten data must not be served")
        np.testing.assert_array_equal(r.get(150, 50), ramp(150, 50))

    def test_future_and_invalid_reads_return_none(self):
        r = Ring(100)
        r.push(ramp(0, 50))
        self.assertIsNone(r.get(45, 10), "cannot read past what was written")
        self.assertIsNone(r.get(-1, 5))
        self.assertIsNone(r.get(10, 0))

    def test_push_larger_than_capacity_keeps_the_tail(self):
        r = Ring(64)
        r.push(ramp(0, 200))
        self.assertEqual(r.written, 200)
        np.testing.assert_array_equal(r.get(136, 64), ramp(136, 64))

    def test_reset_forgets_everything(self):
        # Called on retune. Serving IQ from the previous centre would attribute
        # one band's signal to another band's frequency.
        r = Ring(100)
        r.push(ramp(0, 80))
        r.reset()
        self.assertEqual(r.written, 0)
        self.assertIsNone(r.get(0, 10), "nothing has been written since reset")
        r.push(ramp(500, 30))
        np.testing.assert_array_equal(r.get(0, 30), ramp(500, 30))
        self.assertTrue(np.all(r.buf[30:] == 0), "stale samples must be zeroed")


if __name__ == "__main__":
    unittest.main()
