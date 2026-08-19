"""analyze_analog: what one channel's IQ turns into.

The window handed to this function is not the transmission. It starts a
pretrigger early and, for a short keyup, ends late — so a large part of it can be
carrier-free. An FM discriminator fed noise returns values spread uniformly over
+/- audio_fs/2, so an untrimmed window reports the noise figure as the deviation.
That was true of every event the deck logged while the function measured to
within 4% when tested on pure signal, which is why these tests feed it windows
shaped like the ones the capture loop actually produces.
"""

import unittest

import support  # noqa: F401  — puts src/ on sys.path

import numpy as np


import dcs
import survey_prototype as proto

RATE = 2.4e6
OFFSET = 12500.0

# Every third tone, plus the two that a regression actually broke. Sweeping all
# 54 costs a minute and a half here and buys nothing the full sweep in
# --selftest does not already cover.
TONE_SAMPLE = sorted(set(list(proto.CTCSS_TONES[::3]) + [110.9, 254.1]))


def noise_block(seconds, level=0.05, seed=0):
    n = int(seconds * RATE)
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n))
            .astype(np.complex64) * np.float32(level))


def window(signal_s=1.0, lead_s=0.0, trail_s=0.0, deviation=2400.0,
           tone=None, noise=0.05, seed=1):
    """A capture window shaped the way the ring buffer hands one over."""
    sig = proto.make_fm(tone, RATE, dur=signal_s, offset=OFFSET,
                        voice_dev=deviation, noise=noise, seed=seed)
    parts = []
    if lead_s:
        parts.append(noise_block(lead_s, noise or 0.05, seed + 10))
    parts.append(sig)
    if trail_s:
        parts.append(noise_block(trail_s, noise or 0.05, seed + 20))
    return np.concatenate(parts)


class TestSignalTrim(unittest.TestCase):
    """Deviation must describe the carrier, not the window it arrived in."""

    def test_lead_in_noise_does_not_inflate_deviation(self):
        base = proto.analyze_analog(window(1.0), RATE, OFFSET)["deviation_hz"]
        for lead in (0.2, 0.4, 0.8):
            got = proto.analyze_analog(window(1.0, lead_s=lead),
                                       RATE, OFFSET)["deviation_hz"]
            self.assertAlmostEqual(got, base, delta=250,
                                   msg=f"{lead}s lead-in shifted deviation")

    def test_trailing_noise_does_not_inflate_deviation(self):
        # The close-on-end path analyses whatever dwell exists, and the hang time
        # means the window runs past the carrier. Same failure, other end.
        base = proto.analyze_analog(window(1.0), RATE, OFFSET)["deviation_hz"]
        for trail in (0.2, 0.4, 0.8):
            got = proto.analyze_analog(window(1.0, trail_s=trail),
                                       RATE, OFFSET)["deviation_hz"]
            self.assertAlmostEqual(got, base, delta=250,
                                   msg=f"{trail}s of trailing noise shifted it")

    def test_noise_on_both_sides(self):
        base = proto.analyze_analog(window(1.0), RATE, OFFSET)["deviation_hz"]
        got = proto.analyze_analog(window(1.0, lead_s=0.4, trail_s=0.4),
                                   RATE, OFFSET)["deviation_hz"]
        self.assertAlmostEqual(got, base, delta=250)

    def test_untrimmed_noise_would_have_read_as_wide(self):
        # Establishes that the guard above is guarding something: the noise this
        # trims away really does sit near audio_fs/2, far above the FRS limit.
        pure_noise = noise_block(1.0)
        got = proto.analyze_analog(pure_noise, RATE, OFFSET)
        self.assertGreater(got["deviation_hz"], 8000.0,
                           "noise should read enormous; if not, this test is weak")

    def test_analyzed_s_reports_signal_not_window(self):
        got = proto.analyze_analog(window(0.8, lead_s=0.6), RATE, OFFSET)
        self.assertAlmostEqual(got["analyzed_s"], 0.8, delta=0.12)

    def test_a_burst_in_the_lead_in_does_not_defeat_the_trim(self):
        # The trim used to take its edges from the first and last sample over
        # threshold, so any interference early in the pretrigger anchored it at
        # the start of the window and the whole lead-in came back in. The
        # deviation then reported ~11500 Hz — the noise figure — which is the
        # exact failure the trim exists to prevent, and "wide" is the verdict
        # that rules FRS out. Sub-millisecond is enough; it survives the
        # decimation filter, unlike a single sample.
        base = proto.analyze_analog(window(0.9), RATE, OFFSET)["deviation_hz"]
        for burst_ms in (0.05, 1.0, 5.0):
            iq = window(0.9, lead_s=0.5)
            start = int(0.0001 * RATE)
            iq[start:start + int(burst_ms / 1000 * RATE)] = 5.0 + 0j
            got = proto.analyze_analog(iq, RATE, OFFSET)["deviation_hz"]
            self.assertAlmostEqual(
                got, base, delta=300,
                msg=f"a {burst_ms} ms burst in the lead-in moved deviation to "
                    f"{got:.0f} Hz")

    def test_a_tone_survives_the_trim(self):
        got = proto.analyze_analog(window(1.2, lead_s=0.4, tone=141.3),
                                   RATE, OFFSET)
        self.assertEqual(got["ctcss_hz"], 141.3)


class TestDwellGating(unittest.TestCase):
    """Each stage is gated on its own dwell, not on one all-or-nothing threshold."""

    def test_deviation_is_available_at_very_short_dwell(self):
        # This is what lets a "copy that" reach tier 1 instead of tier 0.
        got = proto.analyze_analog(window(0.25), RATE, OFFSET)
        self.assertIsNotNone(got["deviation_hz"])
        self.assertGreater(got["deviation_hz"], 1500)
        self.assertLess(got["deviation_hz"], 4000)
        self.assertFalse(got["tone_checked"], "0.25 s is too short for a tone")

    def test_deviation_is_stable_across_dwell(self):
        # Measured: 0.10 s reads the same as 1.40 s. If that stops being true the
        # short-transmission path is reporting something different in kind from
        # the long one, and the two are compared against one threshold.
        values = [proto.analyze_analog(window(d), RATE, OFFSET)["deviation_hz"]
                  for d in (0.2, 0.5, 1.0, 1.4)]
        self.assertLess(max(values) - min(values), 400,
                        f"deviation drifts with dwell: {values}")

    def test_tone_checked_flag_marks_the_boundary(self):
        short = proto.analyze_analog(window(proto.MIN_TONE_SECONDS - 0.15),
                                     RATE, OFFSET)
        long = proto.analyze_analog(window(proto.MIN_TONE_SECONDS + 0.35),
                                    RATE, OFFSET)
        self.assertFalse(short["tone_checked"])
        self.assertTrue(long["tone_checked"])

    def test_unchecked_is_not_the_same_claim_as_no_tone(self):
        # A short keyup that was never examined must not be recorded as a channel
        # confirmed clean; the tier ladder turns on the difference.
        got = proto.analyze_analog(window(0.3, tone=141.3), RATE, OFFSET)
        self.assertFalse(got["tone_checked"])
        self.assertIsNone(got["ctcss_hz"])

    def test_far_too_short_returns_nothing(self):
        self.assertIsNone(proto.analyze_analog(
            np.zeros(1000, np.complex64), RATE, OFFSET))


class TestToneAndCode(unittest.TestCase):

    def test_confidence_never_exceeds_one(self):
        # events.confidence is CHECK-constrained to [0,1] and the Hann-gain
        # approximation overshoots on a clean strong tone, so unclamped the
        # cleanest possible signal is the one that throws on INSERT.
        worst = 0.0
        for tone in TONE_SAMPLE:
            got = proto.analyze_analog(
                proto.make_fm(tone, RATE, dur=1.0, noise=0.01), RATE, OFFSET)
            self.assertGreaterEqual(got["ctcss_conf"], 0.0)
            self.assertLessEqual(got["ctcss_conf"], 1.0)
            worst = max(worst, got["ctcss_conf"])
        self.assertGreater(worst, 0.9, "test should be reaching the clamp")

    def test_ctcss_is_never_reported_as_dcs(self):
        # A pure tone sliced at 134.4 bps is periodic, so every 23-bit window
        # agrees with every other — exactly the evidence the DCS decoder treats
        # as proof. With the decode run ahead of the capture-ratio test, 110.9 Hz
        # came back as DCS 243 and 254.1 Hz as DCS 031.
        offenders = [t for t in TONE_SAMPLE
                     if proto.analyze_analog(proto.make_fm(t, RATE, dur=1.0),
                                             RATE, OFFSET)["dcs_code"] is not None]
        self.assertEqual(offenders, [])

    def test_dcs_decodes_and_reports_its_error_count(self):
        code = sorted(dcs.STANDARD_CODES)[0]
        bits = np.array([1.0 if b else -1.0
                         for b in dcs.bit_sequence(code, "N")])
        got = proto.analyze_analog(
            proto.make_fm(None, RATE, dcs_word=bits, noise=0.5), RATE, OFFSET)
        self.assertEqual(got["dcs_code"], int(code))
        self.assertEqual(got["dcs_polarity"], "N")
        self.assertIn(got["dcs_errors"], (0, 1, 2, 3))

    def test_an_inverted_transmission_reads_as_its_partner(self):
        # Sending X inverted is the same waveform as sending INVERTED_PAIR[X]
        # normally, so that is the correct answer, not a misread. A radio
        # programmed to either opens on it.
        for code in list(dcs.STANDARD_CODES)[:4]:
            bits = np.array([1.0 if b else -1.0
                             for b in dcs.bit_sequence(code, "I")])
            got = proto.analyze_analog(
                proto.make_fm(None, RATE, dcs_word=bits), RATE, OFFSET)
            self.assertEqual(got["dcs_code"], int(dcs.INVERTED_PAIR[code]),
                             f"{code} sent inverted should read as "
                             f"{dcs.INVERTED_PAIR[code]}")
            self.assertEqual(got["dcs_polarity"], "N",
                             "the normal reading is the canonical one")

    def test_codes_across_the_whole_table_decode(self):
        # Spread over the list rather than the first few, so a construction that
        # only happens to work at one end of the code space fails here.
        for code in list(dcs.STANDARD_CODES)[::16]:
            bits = np.array([1.0 if b else -1.0
                             for b in dcs.bit_sequence(code, "N")])
            got = proto.analyze_analog(
                proto.make_fm(None, RATE, dcs_word=bits, noise=0.5),
                RATE, OFFSET)
            self.assertEqual(got["dcs_code"], int(code))

    def test_random_noise_produces_no_codeword(self):
        for seed in range(6):
            got = proto.analyze_analog(noise_block(1.4, seed=seed), RATE, OFFSET)
            self.assertIsNone(got["dcs_code"])


if __name__ == "__main__":
    unittest.main()
