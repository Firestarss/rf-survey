"""--spectrum: the peak list, the reference level, and the frequency estimate.

Phase 1 is carried out entirely through `--spectrum`. It is also the one path
`--simulate` cannot reach, because `spectrum_capture` imports SoapySDR directly
instead of going through `Radio` — so unlike the capture loop it had never been
executed at all until 2026-08-25, and three of its four documented outputs were
wrong or missing. These tests stand a stub SoapySDR module in front of
`simradio` so the whole diagnostic runs here, with no radio.
"""

import argparse
import contextlib
import io
import re
import sys
import types
import unittest

import support  # noqa: F401  — puts src/ on sys.path

import numpy as np

import simradio
import survey_prototype as proto

RATE = 10e6
CENTER = 466_000_000.0
# Exactly on a 6.25 kHz slot, which is what the bench procedure tunes to and the
# worst case for spotting an offset: every error under +/-3125 Hz rounds to it.
ON_GRID = 466_000_000.0

FRS_1_TO_7 = [462_562_500, 462_587_500, 462_612_500, 462_637_500,
              462_662_500, 462_687_500, 462_712_500]


def run_spectrum(transmissions, seconds=8.0, tmp_png="spectrum.png"):
    """Drive spectrum_capture against a simulated radio; return its stdout."""
    class Stub(types.ModuleType):
        SOAPY_SDR_RX = 0
        SOAPY_SDR_CF32 = "CF32"
        SOAPY_SDR_OVERFLOW = -4

        @staticmethod
        def Device(_spec):
            return simradio.SimulatedRadio(
                transmissions, rate=RATE, center_hz=CENTER,
                duration_s=1e9, serial="SIM-UHF")

    saved = sys.modules.get("SoapySDR")
    sys.modules["SoapySDR"] = Stub("SoapySDR")
    try:
        args = argparse.Namespace(
            driver="airspy", serial=None, rate=RATE, freq=CENTER, gain=12.0,
            ppm=0.0, spectrum=tmp_png, spectrum_seconds=seconds)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            proto.spectrum_capture(args)
        return buf.getvalue()
    finally:
        if saved is None:
            del sys.modules["SoapySDR"]
        else:
            sys.modules["SoapySDR"] = saved


def channels_listed(out):
    """The channel column of the peak list, in MHz."""
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s+(\d+\.\d+) MHz\s+[-+]", line)
        if m:
            rows.append(float(m.group(1)))
    return rows


def measured_hz(out, channel_mhz):
    """The interpolated frequency reported for one channel, in Hz."""
    for line in out.splitlines():
        got = re.findall(r"(\d+\.\d+) MHz", line)
        if len(got) == 2 and abs(float(got[0]) - channel_mhz) < 1e-9:
            return float(got[1]) * 1e6
    return None


class FindPeaks(unittest.TestCase):
    """The local-maximum rule, in isolation."""

    def setUp(self):
        self.freqs = np.arange(100) * proto.CHANNEL_HZ + 462_500_000.0

    def test_skirts_are_not_reported(self):
        """One carrier lights three channels; only the middle one is a signal.

        An FM transmission at 2.5-5 kHz deviation spans about 11 kHz against a
        6.25 kHz grid. Before the local-maximum rule a seven-channel FRS sweep
        printed twenty-one entries, and Gate 1 asks the operator to account for
        every entry in the list.
        """
        power = np.full(100, -100.0)
        power[50] = -60.0
        power[49] = power[51] = -72.0        # skirts, well above the floor
        peaks = proto.find_peaks(power, np.full(100, -100.0), self.freqs)
        self.assertEqual([f for f, _ in peaks], [self.freqs[50]])

    def test_real_neighbours_both_survive(self):
        """12.5 kHz apart is two channels apart, and both are real signals.

        This is the constraint that fixes the rule at +/-1 channel: FRS primary
        and interstitial channels interleave to 12.5 kHz, so a wider rule would
        discard genuine traffic as a skirt.
        """
        power = np.full(100, -100.0)
        power[50] = power[52] = -60.0
        power[51] = -75.0
        peaks = proto.find_peaks(power, np.full(100, -100.0), self.freqs)
        self.assertEqual(sorted(f for f, _ in peaks),
                         [self.freqs[50], self.freqs[52]])

    def test_weak_signal_not_crowded_out_by_skirts(self):
        """A real weak signal outranks the skirt of a strong one.

        `top` caps the list. Skirts filling it push out the entries actually
        worth looking at, which is backwards — step 13 exists to find the
        signals nobody expected.
        """
        power = np.full(100, -100.0)
        power[50], power[49], power[51] = -50.0, -62.0, -62.0
        power[80] = -85.0                    # genuine, weaker than the skirts
        peaks = proto.find_peaks(power, np.full(100, -100.0), self.freqs,
                                 top=2)
        self.assertIn(self.freqs[80], [f for f, _ in peaks])


class RefinePeak(unittest.TestCase):
    """Sub-bin frequency estimation, against known offsets."""

    def spectrum_with_tone(self, offset_hz, nfft=proto.NFFT):
        bin_hz = RATE / nfft
        base = CENTER - RATE / 2.0
        n = np.arange(nfft)
        sig = np.exp(2j * np.pi * (offset_hz / RATE) * n) * np.hanning(nfft)
        spec = np.fft.fftshift(np.abs(np.fft.fft(sig)) ** 2)
        return proto.to_db(spec + 1e-20), base, bin_hz

    def test_resolves_far_inside_one_bin(self):
        """Bins are 2441 Hz at 10 MSPS; the gate wants +/-470 Hz.

        Without interpolation the answer is the channel slot, and every error
        the gate cares about reports as exactly zero.
        """
        worst = 0.0
        for offset in (0.0, 120.0, -300.0, 470.0, -470.0, 1200.0):
            spec, base, bin_hz = self.spectrum_with_tone(offset)
            got = proto.refine_peak_hz(spec, base, bin_hz, CENTER + offset)
            self.assertIsNotNone(got, f"no estimate at {offset} Hz")
            worst = max(worst, abs(got - (CENTER + offset)))
        # Measured at 42 Hz through the full capture path. 200 Hz leaves room
        # for a different FFT length or window without hiding a regression.
        self.assertLess(worst, 200.0, f"worst error {worst:.0f} Hz")

    def test_declines_at_the_array_edge(self):
        """No neighbours to sit the parabola on means no answer, not a guess."""
        spec, base, bin_hz = self.spectrum_with_tone(0.0)
        edge = base                       # first bin
        self.assertIsNone(proto.refine_peak_hz(spec, base, bin_hz, edge))

    def test_declines_when_there_is_no_maximum(self):
        """A monotone slope has no apex; interpolating one invents a signal."""
        spec = np.linspace(-100.0, -50.0, 64)
        got = proto.refine_peak_hz(spec, CENTER, 2441.40625,
                                   CENTER + 32 * 2441.40625)
        self.assertIsNone(got)


class SpectrumOutput(unittest.TestCase):
    """What the bench operator actually reads off the terminal."""

    @classmethod
    def setUpClass(cls):
        cls.sweep = run_spectrum(
            [simradio.Transmission(f, 1.0 + 0.9 * i, 0.8, snr_db=30,
                                   label=f"FRS {i + 1}")
             for i, f in enumerate(FRS_1_TO_7)],
            seconds=8.0)

    def test_reference_level_is_printed(self):
        """Steps 6 and 11 both say to record it, and it never appeared.

        The gain is set by raising it until the antenna lifts this level 8-10 dB
        over the dummy load, so Gate 1's delta row could not be filled in. The
        peaks are quoted relative to it, which left the absolute level nowhere
        in the output.
        """
        m = re.search(r"band reference level\s+(-?\d+\.\d+) dB", self.sweep)
        self.assertIsNotNone(m, f"no reference level in:\n{self.sweep}")
        self.assertLess(float(m.group(1)), 0.0)

    def test_seven_keyups_list_as_seven_channels(self):
        """Step 8's sweep. This printed twenty-one entries for seven keyups."""
        listed = channels_listed(self.sweep)
        self.assertEqual(sorted(listed),
                         sorted(f / 1e6 for f in FRS_1_TO_7))

    def test_reports_clock_error_rather_than_the_grid(self):
        """Step 12: a carrier off the slot must read as off the slot.

        The simulator has no clock error, so every row should sit within a few
        tens of Hz of nominal — the point being that the number is a
        measurement and not a rounding of one.
        """
        for f in FRS_1_TO_7:
            got = measured_hz(self.sweep, f / 1e6)
            self.assertIsNotNone(got, f"no estimate for {f}")
            self.assertLess(abs(got - f), 200.0,
                            f"{f}: measured {got}")


class OffFrequencyCarrier(unittest.TestCase):
    """One transmitter genuinely off channel, which is what step 12 measures."""

    def test_offset_carrier_is_measured_not_rounded(self):
        offset = 900.0                     # well under half a channel
        out = run_spectrum([simradio.Transmission(
            ON_GRID + offset, 0.0, 1e6, deviation_hz=0.0, snr_db=40,
            label="CW")], seconds=6.0)
        got = measured_hz(out, ON_GRID / 1e6)
        self.assertIsNotNone(got, f"carrier not listed:\n{out}")
        self.assertAlmostEqual(got - ON_GRID, offset, delta=200.0)


if __name__ == "__main__":
    unittest.main()
