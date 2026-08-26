"""Reading the profile.

The profile was being snapshotted verbatim into every run row while every setting
actually came from the command line, so a run recorded a configuration it had not
followed. That is worse than recording none, because the snapshot reads as
evidence six months later. These tests pin what the code takes from the file.
"""

import textwrap
import unittest

from support import PROFILE, TempDirCase

from survey_prototype import load_receiver_config


class TestRealProfile(unittest.TestCase):
    """Against profiles/festival.yaml as it actually ships."""

    def test_receivers_are_grouped_by_required_attenuation(self):
        """446 sits with 466, not with the VHF windows.

        Phase 1 measured what each band needs: 446 and 466 want 4-5 dB, 146 and
        155 want 17-20 dB. Grouped the old way — ham with ham, business with
        business — no single pad could serve the vhf receiver, and the failure
        mode of under-attenuating it is silent front-end compression rather than
        anything the overload counters report.
        """
        uhf = load_receiver_config(str(PROFILE), "uhf")
        vhf = load_receiver_config(str(PROFILE), "vhf")
        self.assertEqual({w["center_hz"] for w in uhf["windows"]},
                         {466_000_000, 446_000_000})
        self.assertEqual({w["center_hz"] for w in vhf["windows"]},
                         {146_000_000, 154_950_000})

    def test_uhf_rotates_with_lopsided_dwell(self):
        """466 is the band the survey is for; 446 is sampled, not watched."""
        cfg = load_receiver_config(str(PROFILE), "uhf")
        self.assertEqual(cfg["mode"], "rotating")
        dwell = {w["center_hz"]: w["dwell_s"] for w in cfg["windows"]}
        self.assertEqual(dwell[466_000_000], 300.0)
        self.assertEqual(dwell[446_000_000], 60.0)
        self.assertGreater(dwell[466_000_000], dwell[446_000_000],
                           "the main band must not lose time to the ham segment")

    def test_vhf_rotates_through_every_window(self):
        cfg = load_receiver_config(str(PROFILE), "vhf")
        self.assertEqual(cfg["mode"], "rotating")
        self.assertEqual(len(cfg["windows"]), 2)
        self.assertEqual(cfg["dwell_seconds"], 180.0)

    def test_windows_without_dwell_fall_back_to_the_receiver(self):
        cfg = load_receiver_config(str(PROFILE), "vhf")
        for w in cfg["windows"]:
            self.assertIsNone(w["dwell_s"],
                              "vhf windows should inherit the receiver dwell")

    def test_underscored_numbers_parse_as_numbers(self):
        # The profile uses 466_000_000. That is YAML 1.1 behaviour; under a 1.2
        # parser these become strings and every frequency silently breaks.
        cfg = load_receiver_config(str(PROFILE), "uhf")
        self.assertIsInstance(cfg["windows"][0]["center_hz"], int)
        self.assertIsInstance(cfg["sample_rate"], float)
        self.assertEqual(cfg["sample_rate"], 10_000_000.0)

    def test_detection_thresholds_come_from_the_profile(self):
        cfg = load_receiver_config(str(PROFILE), "uhf")
        self.assertEqual(cfg["on_db"], 10.0)
        self.assertEqual(cfg["off_db"], 6.0)

    def test_serial_is_surfaced_even_when_unset(self):
        # Both serials are null until Phase 1. That is exactly when the deck can
        # address the wrong radio, so the value has to reach the caller rather
        # than being defaulted away.
        self.assertIn("serial", load_receiver_config(str(PROFILE), "uhf"))


class TestMalformedProfiles(TempDirCase):

    def write(self, body):
        path = self.path("p.yaml")
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def test_unknown_receiver_is_refused_with_the_known_names(self):
        path = self.write("""
            receivers:
              uhf: {mode: parked, center_hz: 466000000}
        """)
        with self.assertRaises(SystemExit) as cm:
            load_receiver_config(path, "vhf")
        self.assertIn("uhf", str(cm.exception), "should say what names exist")

    def test_rotating_with_no_windows_is_refused(self):
        path = self.write("""
            receivers:
              vhf: {mode: rotating, dwell_seconds: 60}
        """)
        with self.assertRaises(SystemExit):
            load_receiver_config(path, "vhf")

    def test_parked_with_no_centre_is_refused(self):
        path = self.write("""
            receivers:
              uhf: {mode: parked}
        """)
        with self.assertRaises(SystemExit):
            load_receiver_config(path, "uhf")

    def test_missing_receivers_section_is_refused(self):
        with self.assertRaises(SystemExit):
            load_receiver_config(self.write("name: empty\n"), "uhf")

    def test_defaults_apply_when_optional_keys_are_absent(self):
        path = self.write("""
            receivers:
              uhf: {mode: parked, center_hz: 466000000}
        """)
        cfg = load_receiver_config(path, "uhf")
        self.assertEqual(cfg["sample_rate"], 10e6)
        self.assertEqual(cfg["gain"], 12.0)
        self.assertEqual(cfg["ppm"], 0.0)
        self.assertEqual(cfg["on_db"], 10.0)
        self.assertIsNone(cfg["dwell_seconds"])

    def test_a_zero_gain_is_kept_rather_than_defaulted(self):
        # 0 is a legitimate gain and a falsy value; defaulting it to 12 would
        # silently run the front end 12 dB hotter than asked.
        path = self.write("""
            receivers:
              uhf: {mode: parked, center_hz: 466000000, gain: 0}
        """)
        self.assertEqual(load_receiver_config(path, "uhf")["gain"], 0.0)


if __name__ == "__main__":
    unittest.main()
