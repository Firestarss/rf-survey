"""The Rust engine must make the same detections as the Python engine.

The Rust engine (engine/) replaces the per-frame hot path of the capture loop.
It is only allowed to replace it if, fed identical samples, it opens and closes
the same events on the same channels at the same sample positions. This test
writes a deterministic simulated stream to a file, runs the REAL Python classes
over it frame by frame — Periodogram, ChannelGrid, OverloadMonitor, Detector —
and runs the engine binary over the same file, then compares every start and
every end.

What is compared is the detector's own output, not a re-implementation of it
inside this test, so a mistake cannot be copied into both sides.

Covered deliberately:
  * a centre exactly on the 6.25 kHz grid — at 10 MSPS every 64th FFT bin then
    lands on an exact .5, and numpy rounds half to even
  * both sample rates the deck uses, which give different frame sizes and so
    different min_frames / hang_frames
  * adjacent-channel skirts (the local-maximum rule), a transmission longer than
    the noise-floor history (the active mask), keyups at the minimum duration,
    and a strong signal among weaker ones
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import numpy as np

import support  # noqa: F401

import simradio
import survey_prototype as sp

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine" / "target" / "release" / "rfsurvey-engine"

SETTINGS = {"on_db": 10.0, "off_db": 6.0, "min_duration_s": 0.12, "hang_s": 0.30}


def stress_scenario(t0=2.0):
    T = simradio.Transmission
    return [
        T(462_675_000, t0, 1.5, ctcss_hz=141.3, deviation_hz=4900, snr_db=34),
        # adjacent-channel neighbours 6.25 kHz apart, overlapping in time
        T(462_562_500, t0 + 0.4, 1.0, deviation_hz=2400, snr_db=28),
        T(462_568_750, t0 + 0.5, 0.8, deviation_hz=2400, snr_db=22),
        # at the minimum duration, and just under it
        T(465_500_000, t0 + 1.0, 0.13, deviation_hz=2400, snr_db=30),
        T(465_600_000, t0 + 1.4, 0.10, deviation_hz=2400, snr_db=30),
        # longer than the 120-frame floor history — the active-mask case
        T(467_850_000, t0 + 0.2, 3.0, deviation_hz=2400, snr_db=36),
        # strong, with weaker traffic around it
        T(464_300_000, t0 + 2.0, 0.9, deviation_hz=2400, snr_db=55),
        T(463_900_000, t0 + 2.1, 0.6, deviation_hz=2400, snr_db=15),
        # two keyups on one channel separated by less than, then more than, hang
        T(466_750_000, t0 + 0.8, 0.4, deviation_hz=2400, snr_db=30),
        T(466_750_000, t0 + 1.35, 0.4, deviation_hz=2400, snr_db=30),
        T(466_750_000, t0 + 2.4, 0.4, deviation_hz=2400, snr_db=30),
    ]


def dump_iq(txs, rate, center, seconds, fs, path):
    radio = simradio.SimulatedRadio(txs, rate=rate, center_hz=center, seed=7)
    buf = np.empty(fs, np.complex64)
    frames = int(np.ceil(seconds * rate / fs))
    with open(path, "wb") as fh:
        for _ in range(frames):
            st = radio.readStream(None, [buf], fs)
            n = st.ret if hasattr(st, "ret") else int(st)
            fh.write(buf[:n].tobytes())
    return frames


def python_events(path, rate, center, fs):
    frame_seconds = fs / rate
    pgram = sp.Periodogram(rate)
    det = sp.Detector(center, rate, frame_seconds, SETTINGS)
    ovl = sp.OverloadMonitor()
    freqs = det.grid.freqs_hz
    written = 0
    out = []
    data = np.fromfile(path, dtype=np.complex64)
    for i in range(0, len(data), fs):
        frame = data[i:i + fs]
        frame_start = written
        written += len(frame)
        psd = pgram(frame)
        if psd is None:
            continue
        power_db = sp.to_db(det.grid.power(psd))
        clipping, desense, _ = ovl.update(frame, power_db)
        stepped = det.step(power_db, frame_start)
        if stepped is None:
            continue
        started, ended = stepped
        tr = det.tracker
        for ch in started:
            out.append(("s", float(freqs[ch]), int(tr.last_start_sample), bool(clipping or desense), None))
        for ch in ended:
            dur = max(0.0, (tr.last_end_sample - tr.start_sample[ch]) / rate)
            out.append(("e", float(freqs[ch]), int(tr.last_end_sample), dur, float(tr.peak_snr[ch])))
    return det.grid, out


def engine_events(path, rate, center, fs, shm):
    cfg = {
        "iq_file": str(path), "rate": rate, "fs_request": fs, "file_mtu": fs,
        "on_db": SETTINGS["on_db"], "off_db": SETTINGS["off_db"],
        "min_duration_s": SETTINGS["min_duration_s"], "hang_s": SETTINGS["hang_s"],
        "pretrigger_s": sp.PRETRIGGER_SECONDS, "analyze_s": sp.ANALYZE_SECONDS,
        "min_analyze_s": sp.MIN_ANALYZE_SECONDS, "ring_slack_s": sp.RING_SLACK_SECONDS,
        "shm_path": str(shm), "stat_seconds": 1e9,
    }
    p = subprocess.Popen([str(ENGINE), json.dumps(cfg)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    p.stdin.write(json.dumps({"cmd": "open", "center_hz": center, "linearity": False}) + "\n")
    p.stdin.flush()
    channels, out, ready = None, [], None
    for line in p.stdout:
        m = json.loads(line)
        if m["t"] == "ready":
            ready = m
        elif m["t"] == "window":
            channels = m["channels"]
        elif m["t"] == "f":
            for a in m["acts"]:
                if a[0] == "s":
                    out.append(("s", channels[a[1]] * sp.CHANNEL_HZ, m["ts"], m["ovl"], None))
                elif a[0] == "e":
                    out.append(("e", channels[a[1]] * sp.CHANNEL_HZ, m["te"], a[2], a[3]))
        elif m["t"] in ("eof", "error", "stall"):
            break
    p.stdin.close()
    p.stdout.close()
    p.wait(timeout=30)
    return ready, channels, out


@unittest.skipUnless(ENGINE.exists(), "engine not built: cargo build --release in engine/")
class EngineEquivalence(unittest.TestCase):

    def _compare(self, txs, rate, center, seconds):
        fs = min(sp.frame_size(rate), 65536)
        with tempfile.TemporaryDirectory() as d:
            iq = pathlib.Path(d) / "stream.iq"
            dump_iq(txs, rate, center, seconds, fs, iq)
            grid, py = python_events(iq, rate, center, fs)
            ready, channels, rs = engine_events(iq, rate, center, fs, pathlib.Path(d) / "ring")

        self.assertEqual(ready["fs"], fs)
        self.assertEqual(ready["min_frames"], max(1, int(round(SETTINGS["min_duration_s"] / (fs / rate)))))
        self.assertEqual(ready["hang_frames"], max(1, int(round(SETTINGS["hang_s"] / (fs / rate)))))
        self.assertEqual(channels, [int(c) for c in grid.channels], "channel grids differ")
        self.assertGreater(len(py), 4, "scenario produced too few events to prove anything")

        self.assertEqual([(k, f, s) for k, f, s, *_ in rs], [(k, f, s) for k, f, s, *_ in py],
                         "starts/ends differ in channel, order or sample position")
        for (k, f, s, a, b), (_, _, _, a2, b2) in zip(py, rs):
            if k == "s":
                self.assertEqual(a, a2, f"overload flag differs at {f} {s}")
            else:
                self.assertAlmostEqual(a, a2, places=9, msg=f"duration differs at {f} {s}")
                self.assertAlmostEqual(b, b2, delta=1e-3, msg=f"peak SNR differs at {f} {s}")
        return py

    def test_stress_10msps_centre_on_grid(self):
        self._compare(stress_scenario(), 10_000_000.0, 466_000_000.0, 5.8)

    def test_stress_2m5msps_off_grid(self):
        self._compare(stress_scenario(), 2_500_000.0, 465_999_702.24, 5.8)

    def test_festival_2m5msps(self):
        # Centred on 462.9, not 466.0: a 2.5 MSPS window only hears +/-1.125 MHz,
        # and every festival UHF transmission is further than that from 466.0.
        # The first version of this test centred it there, both engines
        # correctly saw nothing, and the guard below is what caught it.
        self._compare(simradio.festival_scenario(), 2_500_000.0, 462_900_000.0, 13.5)


if __name__ == "__main__":
    unittest.main()
