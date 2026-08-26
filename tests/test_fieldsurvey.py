"""The field survey tool: privacy codes, geometry, and the fit.

This one runs unattended in a field with no way to check its answers, so the
parts that could be quietly wrong are the parts worth testing — above all the
privacy-code table, which is a different list from the decoder's.
"""

import math
import pathlib
import sys
import unittest

import support  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import fieldsurvey as fs  # noqa: E402

import survey_prototype as proto  # noqa: E402


class PrivacyCodes(unittest.TestCase):
    """The 38-code table is NOT the decoder's 54-tone list."""

    def test_the_codes_confirmed_against_a_real_radio(self):
        # Measured off a Retevis and a Rocky Talkie on 2026-08-26/27: the deck
        # decoded these tones when the radio was set to these code numbers.
        for code, tone in ((3, 74.4), (15, 110.9), (20, 131.8), (21, 136.5),
                           (26, 162.2), (28, 173.8)):
            self.assertEqual(fs.tone_for_code(code), tone, f"code {code}")

    def test_it_is_not_the_decoder_list(self):
        """Indexing CTCSS_TONES by code number is wrong from code 2 upward.

        The decoder knows 54 tones; the radios number 38. The extra entries —
        69.3 first among them — shift everything after. Using the wrong list
        would attribute every point in a survey to the wrong location, and do it
        consistently enough to look like data rather than a bug.
        """
        self.assertEqual(len(fs.CODE_TONES), 38)
        self.assertEqual(len(proto.CTCSS_TONES), 54)
        self.assertNotEqual(fs.tone_for_code(21), float(proto.CTCSS_TONES[20]))
        self.assertEqual(fs.tone_for_code(21), 136.5)
        self.assertAlmostEqual(float(proto.CTCSS_TONES[20]), 131.8)

    def test_every_code_round_trips(self):
        for code in range(1, 39):
            self.assertEqual(fs.code_for_tone(fs.tone_for_code(code)), code)

    def test_a_tone_outside_the_scheme_has_no_code(self):
        self.assertIsNone(fs.code_for_tone(69.3))

    def test_out_of_range_codes_are_refused(self):
        for bad in (0, 39, -1):
            with self.assertRaises(SystemExit):
                fs.tone_for_code(bad)


class Geometry(unittest.TestCase):

    def test_distance_against_a_known_pair(self):
        # The receiver and the gym, from the 2026-08-27 walk: 492 m.
        d = fs.distance_m((42.3854086, -71.0796309),
                          (42.3836177, -71.0741511))
        self.assertAlmostEqual(d, 492, delta=5)

    def test_bearing_cardinals(self):
        o = (42.0, -71.0)
        self.assertAlmostEqual(fs.bearing_deg(o, (42.01, -71.0)), 0, delta=1)
        self.assertAlmostEqual(fs.bearing_deg(o, (42.0, -70.99)), 90, delta=1)
        self.assertAlmostEqual(fs.bearing_deg(o, (41.99, -71.0)), 180, delta=1)
        self.assertAlmostEqual(fs.bearing_deg(o, (42.0, -71.01)), 270, delta=1)

    def test_compass_labels(self):
        for deg, want in ((0, "N"), (90, "E"), (180, "S"), (270, "W"),
                          (359, "N"), (45, "NE")):
            self.assertEqual(fs.compass(deg), want)


class Fit(unittest.TestCase):

    def test_recovers_a_known_exponent(self):
        """30 dB per decade, exactly, must come back as 30."""
        xs = [math.log10(d) for d in (10, 50, 100, 500, 1000)]
        ys = [100.0 - 30.0 * x for x in xs]
        slope, inter, r2, sigma = fs.fit_line(xs, ys)
        self.assertAlmostEqual(-slope, 30.0, places=6)
        self.assertAlmostEqual(inter, 100.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=9)
        self.assertAlmostEqual(sigma, 0.0, places=9)

    def test_reproduces_the_real_walk(self):
        """The 2026-08-27 dataset fitted 30.4 dB/decade at R^2 0.937."""
        pts = [(10, 66.5), (56, 53.3), (109, 37.4), (122, 38.9),
               (209, 22.6), (227, 29.5), (412, 21.2)]
        slope, inter, r2, sigma = fs.fit_line(
            [math.log10(d) for d, _ in pts], [s for _, s in pts])
        self.assertAlmostEqual(-slope, 30.4, delta=0.2)
        self.assertAlmostEqual(r2, 0.937, delta=0.005)
        self.assertLess(sigma, 4.5)

    def test_identical_distances_are_refused(self):
        with self.assertRaises(SystemExit):
            fs.fit_line([1.0, 1.0, 1.0], [10.0, 20.0, 30.0])


if __name__ == "__main__":
    unittest.main()
