"""CaptureStore: the recordings a deployment leaves behind.

`events.audio_path` and `events.iq_path` existed for five schema versions with
nothing writing them. This is the one gap that cannot be closed after the fact —
a festival happens once, and without recordings there is nothing to re-analyse
when a threshold turns out to be wrong.

The budget is tested as carefully as the writing. A deck that fills its disk
mid-festival stops logging events entirely, which is far worse than losing
recordings, so the cap has to hold and it has to fail soft.
"""

import pathlib
import unittest
import wave

import numpy as np

from support import SRC, TempDirCase  # noqa: F401

from survey_prototype import CaptureStore

AUDIO_FS = 24000.0


def signals(seconds=1.0, deviation_hz=2500.0, fs=AUDIO_FS):
    n = int(seconds * fs)
    return {"audio": np.full(n, float(deviation_hz)),
            "baseband": np.ones(n, np.complex64),
            "audio_fs": fs}


class TestWriting(TempDirCase):

    def test_wav_is_well_formed(self):
        store = CaptureStore(self.path("caps"), run_id=3)
        apath, ipath = store.write(42, signals(1.0))
        self.assertIsNone(ipath, "IQ retention is off by default")
        with wave.open(apath) as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), 8000)
            self.assertAlmostEqual(w.getnframes() / 8000.0, 1.0, delta=0.02)

    def test_path_layout_is_by_run_and_event(self):
        store = CaptureStore(self.path("caps"), run_id=7)
        apath, _ = store.write(1234, signals(0.3))
        self.assertEqual(pathlib.Path(apath).parent.name, "run7")
        self.assertEqual(pathlib.Path(apath).name, "00001234.wav")

    def test_deviation_maps_to_a_known_amplitude(self):
        # Full scale is FULL_SCALE_HZ of deviation, so a steady half-scale
        # deviation must come back as a steady half-scale sample. Without a fixed
        # mapping, two recordings cannot be compared to each other.
        store = CaptureStore(self.path("caps"), run_id=1)
        half = CaptureStore.FULL_SCALE_HZ / 2.0
        apath, _ = store.write(1, signals(0.5, deviation_hz=half))
        with wave.open(apath) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        self.assertAlmostEqual(float(np.median(pcm)), 32767 * 0.5, delta=400)

    def test_loud_signals_clip_rather_than_wrap(self):
        # int16 wraparound turns a strong signal into a full-scale square wave,
        # which sounds like a fault and is not one.
        store = CaptureStore(self.path("caps"), run_id=1)
        apath, _ = store.write(1, signals(0.3, deviation_hz=50_000.0))
        with wave.open(apath) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        self.assertGreater(float(np.median(pcm)), 32000)
        self.assertLessEqual(int(pcm.max()), 32767)
        self.assertGreater(int(pcm.min()), 0, "wrapped to negative — clipping failed")

    def test_iq_round_trips(self):
        store = CaptureStore(self.path("caps"), run_id=1, keep_iq=True)
        sig = signals(0.25)
        _, ipath = store.write(9, sig)
        data = np.load(ipath)
        self.assertEqual(data["baseband"].dtype, np.complex64)
        self.assertEqual(len(data["baseband"]), len(sig["baseband"]))
        self.assertAlmostEqual(float(data["fs"]), AUDIO_FS)

    def test_counts_and_bytes_are_tracked(self):
        store = CaptureStore(self.path("caps"), run_id=1)
        for i in range(4):
            store.write(i, signals(0.2))
        self.assertEqual(store.count, 4)
        self.assertGreater(store.written, 0)
        on_disk = sum(f.stat().st_size for f in store.root.iterdir())
        self.assertEqual(store.written, on_disk)


class TestBudget(TempDirCase):

    def test_cap_stops_writing_and_says_so(self):
        store = CaptureStore(self.path("caps"), run_id=1, max_mb=0.02)
        written = []
        for i in range(20):
            apath, _ = store.write(i, signals(1.0))
            if apath:
                written.append(apath)
        self.assertTrue(written, "should have written something before the cap")
        self.assertLess(len(written), 20, "cap was never reached")
        self.assertIsNotNone(store.stopped)
        self.assertIn("budget", store.stopped)

    def test_cap_is_checked_before_writing_not_after(self):
        # Checking afterwards means the cap is exceeded by one file every time,
        # and with IQ retention on that file can be large.
        store = CaptureStore(self.path("caps"), run_id=1, max_mb=0.02)
        for i in range(20):
            store.write(i, signals(1.0))
        self.assertLessEqual(store.written, store.max_bytes)

    def test_once_stopped_it_stays_stopped(self):
        store = CaptureStore(self.path("caps"), run_id=1, max_mb=0.02)
        for i in range(20):
            store.write(i, signals(1.0))
        self.assertEqual(store.write(999, signals(0.01)), (None, None))

    def test_a_generous_budget_does_not_interfere(self):
        store = CaptureStore(self.path("caps"), run_id=1, max_mb=500)
        for i in range(6):
            apath, _ = store.write(i, signals(0.5))
            self.assertIsNotNone(apath)
        self.assertIsNone(store.stopped)


if __name__ == "__main__":
    unittest.main()
