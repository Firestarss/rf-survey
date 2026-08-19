"""The enricher's rules.

These are what turn a pile of detections into "here is what you could talk on",
so a rule that is subtly wrong produces a confident, plausible, wrong answer. The
full fixture chain exercises them end to end; these pin the individual decisions
so a break is attributable rather than just visible.
"""

import unittest

from support import TempDirCase

import db
import enrich


def plan(service, label, *, bandwidth_hz, licensed=1, is_repeater_out=0,
         kind="channel", freq=462_675_000):
    """A band_plan row as a dict, standing in for a sqlite3.Row."""
    return {"service": service, "label": label, "bandwidth_hz": bandwidth_hz,
            "licensed": licensed, "is_repeater_out": is_repeater_out,
            "kind": kind, "freq_center_hz": freq, "id": abs(hash(label)) % 10000}


def ev(deviation_hz, snr_db, **kw):
    row = {"deviation_hz": deviation_hz, "snr_db": snr_db, "duration_s": 1.0,
           "t_start": 1000.0, "freq_hz": 462_675_000, "modulation": "fm",
           "content": None, "tone_state": "unknown", "ctcss_hz": None,
           "dcs_code": None, "dcs_polarity": None}
    row.update(kw)
    return row


FRS = plan("frs", "FRS 20", bandwidth_hz=12_500, licensed=0)
GMRS = plan("gmrs", "GMRS 20", bandwidth_hz=20_000, licensed=1)
TIED = [FRS, GMRS]


class TestDeviationDiscriminator(unittest.TestCase):
    """Wide rules FRS out. Narrow rules nothing out. Weak rules nothing at all."""

    def test_narrow_keeps_both_candidates(self):
        evs = [ev(2400, 30) for _ in range(6)]
        self.assertEqual(enrich._narrow_by_deviation(TIED, evs), TIED,
                         "narrowband GMRS radios are common; narrow proves nothing")

    def test_wide_rules_frs_out(self):
        evs = [ev(4900, 30) for _ in range(6)]
        kept = enrich._narrow_by_deviation(TIED, evs)
        self.assertEqual([t["label"] for t in kept], ["GMRS 20"])

    def test_boundary_is_inclusive_at_limit_plus_margin(self):
        limit = enrich.DEVIATION_LIMIT_HZ[12_500] + enrich.DEV_EVIDENCE_MARGIN_HZ
        at = [ev(limit, 30) for _ in range(6)]
        self.assertEqual(len(enrich._narrow_by_deviation(TIED, at)), 2,
                         "exactly at the threshold must not rule FRS out")
        over = [ev(limit + 1, 30) for _ in range(6)]
        self.assertEqual(len(enrich._narrow_by_deviation(TIED, over)), 1)

    def test_too_few_measurements_changes_nothing(self):
        evs = [ev(4900, 30) for _ in range(enrich.DEV_MIN_EVIDENCE - 1)]
        self.assertEqual(enrich._narrow_by_deviation(TIED, evs), TIED)

    def test_weak_measurements_are_not_evidence(self):
        # The regression case for the SNR gate. A distant FRS handheld measures
        # wide because FM clicks inflate the estimator, and "wide" is the verdict
        # that rules FRS out — so ungated this mislabels the exact radio the rule
        # exists to protect.
        weak = [ev(5400, enrich.DEV_MIN_SNR_DB - 1) for _ in range(8)]
        self.assertEqual(enrich._narrow_by_deviation(TIED, weak), TIED,
                         "sub-gate measurements must not decide anything")

    def test_strong_minority_outvotes_a_weak_majority(self):
        evs = ([ev(5400, 10) for _ in range(8)]          # distant, inflated
               + [ev(2400, 30) for _ in range(4)])       # close, honest
        kept = enrich._narrow_by_deviation(TIED, evs)
        self.assertEqual(len(kept), 2, "the four usable measurements decide")

    def test_single_candidate_is_returned_untouched(self):
        self.assertEqual(enrich._narrow_by_deviation([GMRS], [ev(4900, 30)] * 6),
                         [GMRS])

    def test_candidates_sharing_a_limit_are_untouched(self):
        same = [plan("a", "A", bandwidth_hz=12_500), plan("b", "B", bandwidth_hz=12_500)]
        self.assertEqual(enrich._narrow_by_deviation(same, [ev(9000, 30)] * 6), same)

    def test_null_deviations_are_ignored(self):
        evs = [ev(None, 30) for _ in range(9)]
        self.assertEqual(enrich._narrow_by_deviation(TIED, evs), TIED)

    def test_missing_snr_is_not_treated_as_strong(self):
        evs = [ev(4900, None) for _ in range(9)]
        self.assertEqual(enrich._narrow_by_deviation(TIED, evs), TIED,
                         "a NULL SNR must not pass the gate")


class TestUsableDeviations(unittest.TestCase):

    def test_gate_is_inclusive_at_the_threshold(self):
        gate = enrich.DEV_MIN_SNR_DB
        evs = [ev(2000, gate), ev(3000, gate - 0.1), ev(4000, gate + 0.1)]
        self.assertEqual(enrich._usable_deviations(evs), [2000, 4000])


class TierCase(TempDirCase):
    """score() against channels built by hand, so each rung is isolated."""

    def setUp(self):
        super().setUp()
        self.dbpath = self.path("tiers.sqlite")
        db.init_schema(self.dbpath)
        self.conn = db.connect(self.dbpath)
        self.conn.executemany(
            """INSERT INTO band_plan (id, freq_lo_hz, freq_hi_hz, freq_center_hz,
                   bandwidth_hz, service, label, kind, is_repeater_out, licensed)
               VALUES (?,?,?,?,?,?,?,'channel',?,?)""",
            [(1, 462_670_000, 462_680_000, 462_675_000, 20_000, "gmrs",
              "GMRS 20", 0, 1),
             (2, 146_810_000, 146_830_000, 146_820_000, 20_000, "ham2m",
              "2 m repeater", 1, 1),
             (3, 462_645_000, 462_655_000, 462_650_000, 12_500, "frs",
              "FRS 19", 0, 0)])
        self.conn.commit()

    def channel(self, band_plan_id=1, **kw):
        row = {"freq_hz": 462_675_000, "service": "gmrs", "label": "x",
               "band_plan_id": band_plan_id, "modulation": "fm",
               "tone_state": "unknown", "ctcss_hz": None, "dcs_code": None,
               "deviation_hz": None, "pair_id": None, "event_count": 1,
               "total_airtime_s": 1.0, "first_seen": 1.0, "last_seen": 1.0,
               "rebuilt_at": 1.0}
        row.update(kw)
        cols = ", ".join(row)
        self.conn.execute(
            f"INSERT INTO channels ({cols}) VALUES ({', '.join('?'*len(row))})",
            list(row.values()))
        self.conn.commit()
        return self.conn.execute(
            "SELECT id FROM channels ORDER BY id DESC LIMIT 1").fetchone()["id"]

    def tier_of(self, cid, licences=("gmrs", "ham2m")):
        return enrich.score(self.conn, set(licences))[cid]


class TestTierLadder(TierCase):

    def test_0_heard_but_never_analysed(self):
        self.assertEqual(self.tier_of(self.channel(deviation_hz=None)), 0)

    def test_1_analysed_but_tone_unchecked(self):
        cid = self.channel(deviation_hz=2400, tone_state="unknown")
        self.assertEqual(self.tier_of(cid), 1)

    def test_2_dcs_suspected_without_a_codeword(self):
        # "Probably some DCS" will not programme a radio.
        cid = self.channel(deviation_hz=2400, tone_state="dcs", dcs_code=None)
        self.assertEqual(self.tier_of(cid), 2)

    def test_2_ctcss_claimed_without_a_value(self):
        cid = self.channel(deviation_hz=2400, tone_state="ctcss", ctcss_hz=None)
        self.assertEqual(self.tier_of(cid), 2)

    def test_3_programmable_but_not_licensed(self):
        cid = self.channel(deviation_hz=2400, tone_state="ctcss", ctcss_hz=141.3)
        self.assertEqual(self.tier_of(cid, licences=()), 3)

    def test_4_programmable_and_licensed(self):
        cid = self.channel(deviation_hz=2400, tone_state="ctcss", ctcss_hz=141.3)
        self.assertEqual(self.tier_of(cid), 4)

    def test_4_via_decoded_dcs(self):
        cid = self.channel(deviation_hz=2400, tone_state="dcs", dcs_code=23)
        self.assertEqual(self.tier_of(cid), 4)

    def test_4_on_an_unlicensed_service_without_holding_anything(self):
        cid = self.channel(band_plan_id=3, service="frs", freq_hz=462_650_000,
                           deviation_hz=2400, tone_state="none")
        self.assertEqual(self.tier_of(cid, licences=()), 4,
                         "FRS needs no licence")

    def test_repeater_output_without_an_observed_input_stops_at_2(self):
        # The decision of record: tier 3 requires the input to have been HEARD,
        # not inferred from a standard offset. 146.820's input is 600 kHz down
        # and outside the parked window.
        cid = self.channel(band_plan_id=2, service="ham2m", freq_hz=146_820_000,
                           deviation_hz=2400, tone_state="none", pair_id=None)
        self.assertEqual(self.tier_of(cid), 2)

    def test_repeater_output_with_an_observed_input_reaches_4(self):
        out = self.channel(band_plan_id=2, service="ham2m", freq_hz=146_820_000,
                           deviation_hz=2400, tone_state="none")
        inp = self.channel(band_plan_id=2, service="ham2m", freq_hz=146_220_000,
                           deviation_hz=2400, tone_state="none")
        self.conn.execute(
            "INSERT INTO pairs (output_channel_id, input_channel_id, offset_hz, "
            "evidence_count, confidence, inferred_at) VALUES (?,?,?,?,?,?)",
            (out, inp, -600_000, 9, 1.0, 1.0))
        pid = self.conn.execute("SELECT id FROM pairs").fetchone()["id"]
        self.conn.execute("UPDATE channels SET pair_id = ? WHERE id = ?", (pid, out))
        self.conn.commit()
        self.assertEqual(self.tier_of(out), 4)

    def test_content_no_longer_gates_anything(self):
        # The old ladder required `content`, which nothing in the pipeline ever
        # determines, so every channel capped at tier 1 against real data.
        cid = self.channel(deviation_hz=2400, tone_state="none", content=None)
        self.assertEqual(self.tier_of(cid), 4)


if __name__ == "__main__":
    unittest.main()
