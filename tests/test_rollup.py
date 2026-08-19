"""rollup() and pair(): turning events into channels, and finding repeaters.

Both have crashed on rows the schema explicitly allows. An unattended deck loses
power mid-transmission, which leaves an event with a NULL t_end and duration —
migration 5 exists to permit exactly that — and both passes have to survive it.
"""

import unittest

from support import TempDirCase

import db
import enrich


class RollupCase(TempDirCase):

    def setUp(self):
        super().setUp()
        self.dbpath = self.path("roll.sqlite")
        db.init_schema(self.dbpath)
        self.conn = db.connect(self.dbpath)
        self.conn.executemany(
            """INSERT INTO band_plan (freq_lo_hz, freq_hi_hz, freq_center_hz,
                   bandwidth_hz, service, label, kind, is_repeater_out,
                   licensed, pair_offset_hz)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(462_670_000, 462_680_000, 462_675_000, 20_000, "gmrs",
              "GMRS 20", "channel", 1, 1, 5_000_000),
             (467_670_000, 467_680_000, 467_675_000, 20_000, "gmrs",
              "GMRS 20 input", "channel", 0, 1, None),
             # A segment, not a channel: no nominal frequency, so rollup has to
             # report what was measured.
             (144_000_000, 148_000_000, None, None, "ham2m",
              "2 m", "segment", 0, 1, None)])
        self.conn.execute("INSERT INTO runs (started_at, profile_name, profile_yaml)"
                          " VALUES (1000.0,'t','x: 1')")
        self.conn.commit()

    def add(self, freq_hz, t_start, duration_s=2.0, **kw):
        rx = kw.pop("rx", "uhf")
        fields = {"t_start": t_start, "freq_hz": freq_hz,
                  "deviation_hz": 2400.0, "snr_db": 30.0, "modulation": "fm"}
        if duration_s is not None:
            fields["t_end"] = t_start + duration_s
            fields["duration_s"] = duration_s
        fields.update({k: v for k, v in kw.items() if v is not None})
        return db.log_event(self.conn, 1, rx, **fields)

    def rollup(self):
        enrich.tag(self.conn)
        enrich.rollup(self.conn)
        self.conn.commit()
        return {r["freq_hz"]: r for r in
                self.conn.execute("SELECT * FROM channels")}


class TestInFlightRows(RollupCase):

    def test_rollup_survives_a_null_duration(self):
        for i in range(3):
            self.add(462_675_000, 1000.0 + i * 10)
        self.add(462_675_000, 1100.0, duration_s=None)   # power cut
        chans = self.rollup()
        row = chans[462_675_000]
        self.assertEqual(row["event_count"], 4, "the open row is still evidence")
        self.assertAlmostEqual(row["total_airtime_s"], 6.0,
                               msg="a NULL duration must not be counted as time")

    def test_rollup_survives_every_row_being_open(self):
        for i in range(3):
            self.add(462_675_000, 1000.0 + i * 10, duration_s=None)
        row = self.rollup()[462_675_000]
        self.assertEqual(row["event_count"], 3)
        self.assertAlmostEqual(row["total_airtime_s"], 0.0)

    def test_pair_survives_an_open_output_row(self):
        # pair() bounded its inner loop on o["t_end"], which is NULL here. That
        # crashed the whole enricher on the first power cut.
        for i in range(6):
            t = 1000.0 + i * 100
            self.add(467_675_000, t)              # input, keyed first
            self.add(462_675_000, t + 0.06)       # output follows
        self.add(462_675_000, 2000.0, duration_s=None)
        self.rollup()
        self.assertEqual(enrich.pair(self.conn), 1)


class TestChannelFrequency(RollupCase):

    def test_a_band_plan_channel_reports_its_nominal_frequency(self):
        for i in range(4):
            self.add(462_675_000 + (i - 2) * 300, 1000.0 + i)
        self.assertIn(462_675_000, self.rollup(),
                      "a discrete channel reports the number you would programme")

    def test_a_segment_reports_what_was_measured_not_the_bin_centre(self):
        # 146.820 sits inside the 2 m segment with no channel entry, so it goes
        # through FREQ_BIN_HZ. The bin grid has arbitrary phase against real
        # allocations: reporting the bin centre filed this repeater output as
        # 146.8187, which is 1.25 kHz off and not a frequency anyone transmits on.
        for i in range(5):
            self.add(146_820_000 + (i - 2) * 100, 1000.0 + i, rx="vhf")
        freqs = list(self.rollup())
        self.assertEqual(len(freqs), 1, "the five detections are one channel")
        self.assertAlmostEqual(freqs[0], 146_820_000, delta=200)

    def test_nearby_measurements_group_into_one_channel(self):
        for i in range(6):
            self.add(146_470_000 + (i - 3) * 400, 1000.0 + i, rx="vhf")
        self.assertEqual(len(self.rollup()), 1)

    def test_separate_frequencies_stay_separate(self):
        for i in range(4):
            self.add(146_470_000, 1000.0 + i, rx="vhf")
            self.add(146_600_000, 1000.0 + i, rx="vhf")
        self.assertEqual(len(self.rollup()), 2)


class TestToneAgreement(RollupCase):

    def _tone(self, states):
        for i, (state, ctcss, code) in enumerate(states):
            self.add(462_675_000, 1000.0 + i * 10, tone_state=state,
                     ctcss_hz=ctcss, dcs_code=code)
        return self.rollup()[462_675_000]

    def test_consistent_ctcss_is_recorded(self):
        row = self._tone([("ctcss", 141.3, None)] * 6)
        self.assertEqual(row["tone_state"], "ctcss")
        self.assertEqual(row["ctcss_hz"], 141.3)

    def test_contested_ctcss_becomes_unknown(self):
        # Three groups on one shared FRS channel. Recording the plurality would
        # have you programme a radio the other two thirds never hear.
        row = self._tone([("ctcss", 67.0, None), ("ctcss", 100.0, None),
                          ("ctcss", 141.3, None), ("ctcss", 67.0, None),
                          ("ctcss", 100.0, None), ("ctcss", 141.3, None)])
        self.assertEqual(row["tone_state"], "unknown")
        self.assertIsNone(row["ctcss_hz"])

    def test_suspected_dcs_with_no_codeword_stays_dcs(self):
        # Knowing there is a subaudible signal that is not CTCSS is knowledge,
        # and it is what caps the channel at tier 2. Collapsing it to "unknown"
        # made that rung unreachable, because with no decoder there is never a
        # codeword to agree about.
        row = self._tone([("dcs", None, None)] * 6)
        self.assertEqual(row["tone_state"], "dcs")
        self.assertIsNone(row["dcs_code"])

    def test_consistent_dcs_codeword_is_recorded(self):
        row = self._tone([("dcs", None, 23)] * 6)
        self.assertEqual(row["tone_state"], "dcs")
        self.assertEqual(row["dcs_code"], 23)

    def test_contested_dcs_codewords_become_unknown(self):
        row = self._tone([("dcs", None, 23), ("dcs", None, 31),
                          ("dcs", None, 43), ("dcs", None, 23),
                          ("dcs", None, 31), ("dcs", None, 43)])
        self.assertEqual(row["tone_state"], "unknown")
        self.assertIsNone(row["dcs_code"])

    def test_unchecked_events_do_not_dilute_the_verdict(self):
        # Short transmissions are never examined for a tone. They must not count
        # as votes for "no tone".
        row = self._tone([("ctcss", 141.3, None)] * 4
                         + [("unknown", None, None)] * 8)
        self.assertEqual(row["tone_state"], "ctcss")


class TestDerivedRebuild(RollupCase):

    def test_hand_written_notes_survive_a_rebuild(self):
        for i in range(3):
            self.add(462_675_000, 1000.0 + i)
        self.rollup()
        self.conn.execute("UPDATE channels SET notes = ? WHERE freq_hz = ?",
                          ("stage crew, asked us to stay off", 462_675_000))
        self.conn.commit()
        row = self.rollup()[462_675_000]
        self.assertEqual(row["notes"], "stage crew, asked us to stay off")


class TestPairing(RollupCase):

    def test_correlated_keyups_are_a_pair(self):
        for i in range(8):
            t = 1000.0 + i * 100
            self.add(467_675_000, t)
            self.add(462_675_000, t + 0.06)
        self.rollup()
        self.assertEqual(enrich.pair(self.conn), 1)

    def test_the_right_offset_with_uncorrelated_timing_is_not_a_pair(self):
        # The decoy. 5 MHz apart is 5 MHz apart whether or not a repeater links
        # them; what makes it a pair is that keying one produces the other.
        for i in range(8):
            self.add(467_675_000, 1000.0 + i * 100)
            self.add(462_675_000, 5000.0 + i * 100)
        self.rollup()
        self.assertEqual(enrich.pair(self.conn), 0)

    def test_too_little_evidence_is_not_a_pair(self):
        for i in range(enrich.PAIR_MIN_EVIDENCE - 1):
            t = 1000.0 + i * 100
            self.add(467_675_000, t)
            self.add(462_675_000, t + 0.06)
        self.rollup()
        self.assertEqual(enrich.pair(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
