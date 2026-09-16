"""The whole rust-engine path must produce the same database rows as the Python.

test_engine_equivalence.py proves the engine makes the same detections. This
proves the rest: that the orchestrator replays those decisions into the same
events, that the analysis process reads the right IQ out of shared memory, and
that tone, DCS, deviation and measured frequency come out identical.

Both runs go through survey_prototype.py end to end. The Python run uses
--simulate; the rust run reads a file holding exactly the samples --simulate
generates, paced at the sample rate so analysis sees what it would live.

Timestamps are compared relative to the first event, because the Python engine
anchors its sample clock to the moment it ran and the rust run is given a fixed
anchor. Durations come off the sample clock in both and must match exactly.
"""

import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import numpy as np

import support  # noqa: F401

import simradio
import survey_prototype as sp

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine" / "target" / "release" / "rfsurvey-engine"

RATE = 2_500_000.0
CENTER = 462_900_000.0     # every festival UHF transmission within +/-1.1 MHz is in view
SECONDS = 13.5

COMMON = ["--freq", str(CENTER), "--rate", str(RATE), "--ppm", "0",
          "--receiver-id", "uhf", "--profile", "profiles/festival.yaml"]

COLUMNS = ("freq_hz", "t_start", "duration_s", "snr_db", "overload", "harmonic_of",
           "tone_state", "ctcss_hz", "dcs_code", "dcs_polarity", "deviation_hz", "freq_raw_hz")


def dump_simulated(path):
    """Exactly the stream `--simulate` reads: seed 0, frames of frame_size(RATE)."""
    fs = sp.frame_size(RATE)
    radio = simradio.SimulatedRadio(simradio.festival_scenario(), rate=RATE,
                                    center_hz=CENTER, duration_s=SECONDS)
    buf = np.empty(fs, np.complex64)
    with open(path, "wb") as fh:
        while True:
            st = radio.readStream(None, [buf], fs)
            n = st.ret
            if n <= 0:
                break
            fh.write(buf[:n].tobytes())


def rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        out = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM events ORDER BY t_start, freq_hz").fetchall()
    finally:
        conn.close()
    if not out:
        return []
    t0 = out[0][1]
    return [dict(zip(COLUMNS, r), t_rel=r[1] - t0) for r in out]


@unittest.skipUnless(ENGINE.exists(), "engine not built: cargo build --release in engine/")
class EngineLoopEquivalence(unittest.TestCase):

    def test_same_rows_as_python_engine(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            py_db, rs_db, iq = d / "python.sqlite", d / "rust.sqlite", d / "stream.iq"

            py = subprocess.run([sys.executable, "src/survey_prototype.py", "--simulate", str(SECONDS),
                                 "--db", str(py_db), *COMMON],
                                cwd=ROOT, capture_output=True, text=True, timeout=300)
            self.assertEqual(py.returncode, 0, py.stderr[-2000:])

            dump_simulated(iq)
            rs = subprocess.run([sys.executable, "src/survey_prototype.py", "--engine", "rust",
                                 "--engine-iq-file", str(iq), "--engine-wall0", "1000000000.0",
                                 "--db", str(rs_db), *COMMON],
                                cwd=ROOT, capture_output=True, text=True, timeout=300)
            self.assertEqual(rs.returncode, 0, rs.stdout[-2000:] + rs.stderr[-2000:])

            a, b = rows(py_db), rows(rs_db)

        self.assertGreaterEqual(len(a), 3, "the scenario should produce several events")
        self.assertEqual(len(a), len(b), f"python {len(a)} events, rust {len(b)}")
        analysed = 0
        for x, y in zip(a, b):
            where = f"{x['freq_hz']/1e6:.4f} MHz at +{x['t_rel']:.2f} s"
            self.assertEqual(x["freq_hz"], y["freq_hz"], where)
            self.assertAlmostEqual(x["t_rel"], y["t_rel"], delta=1e-6, msg=where)
            self.assertAlmostEqual(x["duration_s"], y["duration_s"], places=9, msg=where)
            self.assertAlmostEqual(x["snr_db"], y["snr_db"], delta=1e-3, msg=where)
            self.assertEqual(x["overload"], y["overload"], where)
            self.assertEqual(x["harmonic_of"] is None, y["harmonic_of"] is None, where)
            if x["deviation_hz"] is not None and y["deviation_hz"] is not None:
                analysed += 1
                for k in ("tone_state", "ctcss_hz", "dcs_code", "dcs_polarity", "freq_raw_hz"):
                    self.assertEqual(x[k], y[k], f"{k} differs, {where}")
                self.assertAlmostEqual(x["deviation_hz"], y["deviation_hz"], delta=1e-6, msg=where)
            else:
                self.assertEqual(x["deviation_hz"], y["deviation_hz"],
                                 f"analysed by one engine and not the other, {where}")
        self.assertGreaterEqual(analysed, 2, "analysis results were not compared")


if __name__ == "__main__":
    unittest.main()
