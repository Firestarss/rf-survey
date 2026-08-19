"""The synthetic radio.

Every end-to-end test is only as trustworthy as this generator. It has already
been wrong once: a missing 2*pi in the phase integral multiplied every deviation
by 6.28, which made signals occupy eleven channels instead of two and saturated
the discriminator so that it returned what looked exactly like noise. The deck
code was blamed first. These tests check the generator against the analyser and
against itself, so next time the generator is ruled out quickly.
"""

import unittest

import numpy as np

from support import SRC  # noqa: F401

import dcs
import simradio
import survey_prototype as proto

RATE = 2.4e6
CENTER = 466_000_000.0
IN_BAND = 466_012_500.0        # +12.5 kHz, two channels up


def capture(txs, seconds, rate=RATE, center=CENTER, noise=0.05, chunk=None):
    sim = simradio.SimulatedRadio(txs, rate=rate, center_hz=center, noise=noise)
    n = int(seconds * rate)
    if chunk is None:
        buf = np.empty(n, np.complex64)
        sim.readStream(None, [buf], n)
        return buf
    out = []
    done = 0
    while done < n:
        take = min(chunk, n - done)
        piece = np.empty(take, np.complex64)
        sim.readStream(None, [piece], take)
        out.append(piece.copy())
        done += take
    return np.concatenate(out)


class TestPhaseContinuity(unittest.TestCase):

    def test_chunking_does_not_change_the_signal(self):
        # The bug class this guards: integrating phase per block restarts the
        # integral every block and puts a discontinuity at each frame edge. The
        # detector logs those as events, so the simulation would manufacture
        # traffic that the deck then appears to find.
        tx = [simradio.Transmission(IN_BAND, 0.0, 0.5, ctcss_hz=141.3,
                                    deviation_hz=2400, snr_db=30)]
        whole = capture(tx, 0.5, noise=0.0)
        pieces = capture(tx, 0.5, noise=0.0, chunk=8192)
        # Guard against passing on silence: with noise off, an amplitude derived
        # naively from SNR is zero and this test compares nothing to nothing.
        self.assertGreater(float(np.abs(whole).mean()), 1e-6,
                           "generator produced no signal — test would be vacuous")
        np.testing.assert_allclose(whole, pieces, atol=1e-6,
                                   err_msg="signal differs when read in chunks")

    def test_no_discontinuity_at_block_edges(self):
        # A phase jump shows up as an impulse in the sample-to-sample difference.
        tx = [simradio.Transmission(IN_BAND, 0.0, 0.3, deviation_hz=4900,
                                    snr_db=40)]
        sig = capture(tx, 0.3, noise=0.0, chunk=4096)
        self.assertGreater(float(np.abs(sig).mean()), 1e-6,
                           "generator produced no signal — test would be vacuous")
        step = np.abs(np.diff(sig))
        self.assertGreater(float(np.median(step)), 0.0)
        edges = step[4095::4096]
        self.assertLess(float(np.max(edges)), 5.0 * float(np.median(step)),
                        "sample step at block boundaries is anomalously large")


class TestAgreementWithTheAnalyser(unittest.TestCase):
    """What is generated must be what the analyser measures back."""

    def _analyse(self, **kw):
        dur = kw.pop("dur", 1.4)
        tx = [simradio.Transmission(IN_BAND, 0.0, dur, snr_db=kw.pop("snr_db", 34),
                                    **kw)]
        return proto.analyze_analog(capture(tx, dur), RATE, IN_BAND - CENTER)

    def test_deviation_round_trips(self):
        for want in (1200.0, 2400.0, 4900.0):
            got = self._analyse(deviation_hz=want)["deviation_hz"]
            # p99 of the composite peak sits a little above the nominal figure;
            # what matters is that it tracks and lands on the right side of the
            # FRS/GMRS threshold.
            self.assertGreater(got, want * 0.85, f"{want} Hz read as {got:.0f}")
            self.assertLess(got, want * 1.35, f"{want} Hz read as {got:.0f}")

    def test_deviation_lands_on_the_right_side_of_the_threshold(self):
        import enrich
        thresh = (enrich.DEVIATION_LIMIT_HZ[12_500]
                  + enrich.DEV_EVIDENCE_MARGIN_HZ)
        self.assertLess(self._analyse(deviation_hz=2400.0)["deviation_hz"], thresh)
        self.assertGreater(self._analyse(deviation_hz=4900.0)["deviation_hz"], thresh)

    def test_ctcss_round_trips(self):
        for tone in (67.0, 141.3, 250.3):
            self.assertEqual(self._analyse(ctcss_hz=tone)["ctcss_hz"], tone)

    def test_dcs_round_trips_in_both_polarities(self):
        code = sorted(dcs.UNAMBIGUOUS_CODES)[0]
        for polarity in ("N", "I"):
            got = self._analyse(dcs_code=code, dcs_polarity=polarity)
            self.assertEqual(got["dcs_code"], int(code))
            self.assertEqual(got["dcs_polarity"], polarity)
            self.assertEqual(got["dcs_errors"], 0)

    def test_a_plain_carrier_produces_no_tone(self):
        got = self._analyse(deviation_hz=2400.0)
        self.assertIsNone(got["ctcss_hz"])
        self.assertIsNone(got["dcs_code"])
        self.assertTrue(got["tone_checked"])

    def test_frequency_error_is_near_zero_on_an_exact_channel(self):
        # This is what Phase 1 checks ppm calibration against, so a generator
        # that quietly offsets signals would hide a real calibration error.
        self.assertLess(abs(self._analyse(deviation_hz=2400.0)["freq_error_hz"]),
                        50.0)


class TestLevels(unittest.TestCase):

    def _channel_snr(self, snr_db):
        """In-channel SNR as the detector itself would measure it."""
        tx = [simradio.Transmission(IN_BAND, 0.0, 0.25, deviation_hz=100.0,
                                    snr_db=snr_db)]
        sig = capture(tx, 0.25)
        grid = proto.ChannelGrid(CENTER, RATE)
        window = np.hanning(proto.NFFT).astype(np.float32)
        gain = float(np.sum(window ** 2))
        nseg = len(sig) // proto.NFFT
        segs = sig[:nseg * proto.NFFT].reshape(nseg, proto.NFFT)
        spec = np.fft.fftshift(np.fft.fft(segs * window, axis=1), axes=1)
        psd = (np.abs(spec) ** 2).mean(axis=0) / (gain * RATE)
        power_db = 10.0 * np.log10(grid.power(psd) + 1e-20)
        ch = int(np.argmin(np.abs(grid.freqs_hz - IN_BAND)))
        floor = float(np.median(power_db))
        return power_db[ch] - floor

    def test_requested_snr_is_the_in_channel_snr(self):
        # Treating snr_db as wideband instead makes every signal about 30 dB too
        # strong, and a signal that strong leaks through the window sidelobes
        # into a dozen neighbouring channels and is logged a dozen times.
        for want in (20.0, 30.0, 40.0):
            got = self._channel_snr(want)
            self.assertAlmostEqual(got, want, delta=4.0,
                                   msg=f"asked for {want} dB, measured {got:.1f}")

    def test_channel_width_matches_the_detector(self):
        self.assertEqual(simradio.CHANNEL_HZ, proto.CHANNEL_HZ,
                         "the level calculation depends on these agreeing")


class TestWindowing(unittest.TestCase):

    def test_transmissions_outside_the_window_are_not_emitted(self):
        far = [simradio.Transmission(CENTER + 3e6, 0.0, 0.2, snr_db=60)]
        quiet = capture(far, 0.2, noise=0.05)
        empty = capture([], 0.2, noise=0.05)
        self.assertAlmostEqual(float(np.std(quiet)), float(np.std(empty)),
                               delta=0.01)

    def test_retuning_replays_the_scenario(self):
        # Documented behaviour: each window sees the same traffic, so a rotation
        # test exercises every window rather than only the first.
        sim = simradio.SimulatedRadio([], rate=RATE, center_hz=CENTER)
        buf = np.empty(1024, np.complex64)
        sim.readStream(None, [buf], 1024)
        self.assertEqual(sim.samples_read, 1024)
        sim.setFrequency(0, 0, 146_000_000.0)
        self.assertEqual(sim.samples_read, 0)
        self.assertEqual(sim.getFrequency(0, 0), 146_000_000.0)

    def test_end_of_scenario_reports_no_samples(self):
        sim = simradio.SimulatedRadio([], rate=RATE, center_hz=CENTER,
                                      duration_s=0.01)
        buf = np.empty(int(0.02 * RATE), np.complex64)
        self.assertGreater(sim.readStream(None, [buf], len(buf)).ret, 0)
        self.assertEqual(sim.readStream(None, [buf], len(buf)).ret, 0)

    def test_agc_cannot_be_enabled(self):
        # AGC off is called non-negotiable in the handoff; the stand-in enforces
        # it so a change that switches it on fails here rather than at a festival.
        sim = simradio.SimulatedRadio([], rate=RATE, center_hz=CENTER)
        sim.setGainMode(0, 0, False)
        with self.assertRaises(RuntimeError):
            sim.setGainMode(0, 0, True)


if __name__ == "__main__":
    unittest.main()
