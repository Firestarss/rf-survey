#!/usr/bin/env python3
"""Seed band_plan.

Everything is a range. Discrete channels get a narrow window around their nominal
frequency; ham band plan allocations get wide ones. One lookup handles both.

Channel match windows are computed, not hand-written: each window is the smaller of
half the authorised bandwidth and half the gap to the nearest neighbour in the same
service. That guarantees no two channels in one service overlap, without anyone having
to notice that 151.505 and 151.5125 sit only 7.5 kHz apart.

Ham segments come from the ARRL national plan. ARRL states plainly that locally
coordinated plans take precedence — for Massachusetts that means NESMC. These are
placeholders good enough to label with; replace them when you have the regional plan.

Idempotent. Safe to re-run.

    python3 tools/seed_band_plan.py data/survey.sqlite
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import db as dbmod  # noqa: E402

MHZ = 1_000_000
KHZ = 1_000
GMRS_OFFSET = 5 * MHZ

Row = dict


# ---------------------------------------------------------------------------
# Discrete channels
# ---------------------------------------------------------------------------

def channels() -> list[Row]:
    out: list[Row] = []

    def ch(f, bw, service, label, num=None, rpt=0, offset=None,
           licensed=1, source=None, notes=None):
        out.append(dict(freq_center_hz=f, bandwidth_hz=bw, service=service,
                        label=label, channel_number=num, kind="channel",
                        is_repeater_out=rpt, pair_offset_hz=offset,
                        licensed=licensed, source=source, notes=notes))

    # --- FRS, 22 channels, no licence -------------------------------------
    for n in range(1, 8):
        ch(462_562_500 + (n-1)*25*KHZ, 12_500, "frs", f"FRS {n}", str(n),
           licensed=0, source="FCC 95B", notes="2 W")
    for n in range(8, 15):
        ch(467_562_500 + (n-8)*25*KHZ, 12_500, "frs", f"FRS {n}", str(n),
           licensed=0, source="FCC 95B", notes="0.5 W")
    for n in range(15, 23):
        ch(462_550_000 + (n-15)*25*KHZ, 12_500, "frs", f"FRS {n}", str(n),
           licensed=0, source="FCC 95B", notes="2 W")

    # --- GMRS, same frequencies, licensed, plus repeater inputs -----------
    for n in range(1, 8):
        ch(462_562_500 + (n-1)*25*KHZ, 12_500, "gmrs", f"GMRS {n}", str(n),
           source="FCC 95E", notes="5 W simplex")
    for n in range(8, 15):
        ch(467_562_500 + (n-8)*25*KHZ, 12_500, "gmrs", f"GMRS {n}", str(n),
           source="FCC 95E", notes="0.5 W simplex only")
    legacy = {462_575_000: "White Dot", 462_625_000: "Black Dot",
              462_675_000: "Orange Dot"}
    for n in range(15, 23):
        f = 462_550_000 + (n-15)*25*KHZ
        note = "50 W, repeater output"
        if f in legacy:
            note += f"; legacy {legacy[f]} — preprogrammed on cheap radios"
        ch(f, 25_000, "gmrs", f"GMRS {n}", str(n), rpt=1, offset=GMRS_OFFSET,
           source="FCC 95E", notes=note)
        ch(f + GMRS_OFFSET, 25_000, "gmrs", f"GMRS {n} repeater input", f"{n}R",
           source="FCC 95E", notes="50 W, repeater input only")

    # --- MURS, 5 channels, licence by rule --------------------------------
    for f, bw, dot in ((151_820_000, 11_250, None),
                       (151_880_000, 11_250, None),
                       (151_940_000, 11_250, None),
                       (154_570_000, 20_000, "Blue Dot"),
                       (154_600_000, 20_000, "Green Dot")):
        note = f"{bw/1000:g} kHz bandwidth"
        if dot:
            note += f"; legacy {dot}, moved from Part 90 in 2002"
        ch(f, bw, "murs", f"MURS {f//1000 % 1000:03d}", licensed=0,
           source="FCC 95J", notes=note)

    # --- Part 90 itinerant -------------------------------------------------
    for f, label, offset in (
        (151_505_000, "Itinerant 151.505", None),
        (151_512_500, "Itinerant 151.5125", None),
        (151_625_000, "Red Dot", None),
        (151_700_000, "Itinerant 151.700", None),
        (151_760_000, "Itinerant 151.760", None),
        (151_955_000, "Purple Dot", None),
        (154_515_000, "Brown Dot (VHF)", None),
        (154_540_000, "Yellow Dot (VHF)", None),
        (158_400_000, "Itinerant 158.400", None),
        (451_800_000, "Itinerant 451.800", 5*MHZ),
        (451_812_500, "Itinerant 451.8125", 5*MHZ),
        (456_800_000, "Itinerant 456.800 (input)", None),
        (456_812_500, "Itinerant 456.8125 (input)", None),
        (464_500_000, "Brown Dot (UHF)", 5*MHZ),
        (464_550_000, "Yellow Dot (UHF)", 5*MHZ),
        (467_762_500, "J Dot", None),
        (467_812_500, "K Dot", None),
        (467_850_000, "Silver Star", None),
        (467_875_000, "Gold Star", None),
        (467_900_000, "Red Star", None),
        (467_925_000, "Blue Star", None),
        (469_500_000, "Itinerant 469.500", None),
        (469_550_000, "Itinerant 469.550", None),
    ):
        ch(f, 12_500, "part90", label, rpt=1 if offset else 0, offset=offset,
           source="Part 90 itinerant", notes="licence required, no coordination")

    return out


# ---------------------------------------------------------------------------
# Ham segments — ARRL national plan, pending NESMC
# ---------------------------------------------------------------------------

def segments() -> list[Row]:
    out: list[Row] = []

    def seg(lo, hi, service, label, source="ARRL", notes=None,
            center=None, bw=None, kind="segment", rpt=0, offset=None):
        out.append(dict(freq_lo_hz=lo, freq_hi_hz=hi, freq_center_hz=center,
                        bandwidth_hz=bw, service=service, label=label,
                        channel_number=None, kind=kind, is_repeater_out=rpt,
                        pair_offset_hz=offset, licensed=1, source=source,
                        notes=notes))

    M = MHZ
    # --- 2 m, 144-148 -----------------------------------------------------
    two_m = [
        (144.00, 144.05, "2 m EME (CW)"),
        (144.05, 144.10, "2 m general CW and weak signal"),
        (144.10, 144.20, "2 m EME and weak-signal SSB"),
        (144.20, 144.275, "2 m general SSB"),
        (144.275, 144.30, "2 m propagation beacons"),
        (144.30, 144.50, "2 m OSCAR subband"),
        (144.50, 144.60, "2 m linear translator inputs"),
        (144.60, 144.90, "2 m FM repeater inputs"),
        (144.90, 145.10, "2 m weak signal and FM simplex (packet 145.01–145.09)"),
        (145.10, 145.20, "2 m linear translator outputs"),
        (145.20, 145.50, "2 m FM repeater outputs"),
        (145.50, 145.80, "2 m miscellaneous and experimental"),
        (145.80, 146.00, "2 m OSCAR subband"),
        (146.01, 146.37, "2 m repeater inputs"),
        (146.40, 146.58, "2 m simplex"),
        (146.61, 146.97, "2 m repeater outputs"),
        (147.00, 147.39, "2 m repeater outputs"),
        (147.42, 147.57, "2 m simplex"),
        (147.60, 147.99, "2 m repeater inputs"),
    ]
    for lo, hi, label in two_m:
        rpt = 1 if "repeater outputs" in label else 0
        seg(round(lo*M), round(hi*M), "ham2m", label, rpt=rpt,
            offset=600*KHZ if rpt else None,
            notes="600 kHz standard offset" if rpt else None)

    # Specific frequencies inside those segments. Narrower, so they win.
    seg(146_510_000, 146_530_000, "ham2m", "2 m national FM simplex calling",
        center=146_520_000, bw=20_000, kind="channel",
        notes="146.52 — the one everyone monitors")
    seg(144_380_000, 144_400_000, "ham2m", "APRS (North America)",
        center=144_390_000, bw=20_000, kind="channel", notes="packet, not voice")
    seg(144_190_000, 144_210_000, "ham2m", "2 m national SSB calling",
        center=144_200_000, bw=20_000, kind="channel")

    # --- 70 cm, 420-450 ---------------------------------------------------
    seventy = [
        (420.00, 426.00, "70 cm ATV repeater or simplex"),
        (426.00, 432.00, "70 cm ATV simplex"),
        (432.00, 432.07, "70 cm EME"),
        (432.07, 432.10, "70 cm weak-signal CW"),
        (432.10, 432.30, "70 cm mixed-mode and weak signal"),
        (432.30, 432.40, "70 cm propagation beacons"),
        (432.40, 433.00, "70 cm mixed-mode and weak signal"),
        (433.00, 435.00, "70 cm auxiliary and repeater links"),
        (435.00, 438.00, "70 cm satellite only"),
        (438.00, 444.00, "70 cm ATV repeater input and repeater links"),
        (442.00, 445.00, "70 cm repeater inputs and outputs (local option)"),
        (445.00, 447.00, "70 cm links, repeaters and simplex (local option)"),
        (447.00, 450.00, "70 cm repeater inputs and outputs (local option)"),
    ]
    for lo, hi, label in seventy:
        rpt = 1 if "repeater inputs and outputs" in label else 0
        seg(round(lo*M), round(hi*M), "ham70cm", label, rpt=rpt,
            offset=5*M if rpt else None,
            notes="5 MHz standard offset" if rpt else None)

    seg(445_990_000, 446_010_000, "ham70cm", "70 cm national FM simplex calling",
        center=446_000_000, bw=20_000, kind="channel", notes="446.000")
    seg(432_090_000, 432_110_000, "ham70cm", "70 cm calling frequency",
        center=432_100_000, bw=20_000, kind="channel", notes="SSB/CW")

    return out


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------

def add_windows(chans: list[Row]) -> list[Row]:
    """Give each channel a match window that cannot overlap its neighbours.

    half-window = min(half the authorised bandwidth,
                      half the gap to the nearest channel in the same service)

    Without the second term, FRS 15 at 462.5500 and FRS 1 at 462.5625 would both
    claim 462.5563 — they are only 12.5 kHz apart once the primary and interstitial
    channels are interleaved.
    """
    by_service: dict[str, list[int]] = {}
    for c in chans:
        by_service.setdefault(c["service"], []).append(c["freq_center_hz"])
    for v in by_service.values():
        v.sort()

    for c in chans:
        f, svc = c["freq_center_hz"], c["service"]
        peers = by_service[svc]
        i = peers.index(f)
        gaps = []
        if i > 0:
            gaps.append(f - peers[i-1])
        if i < len(peers) - 1:
            gaps.append(peers[i+1] - f)
        half = c["bandwidth_hz"] // 2
        if gaps:
            half = min(half, min(gaps) // 2)
        # Upper bound is one Hz short: both bounds are inclusive in the lookup, so
        # f + half would be claimed by this channel and by the next one up.
        c["freq_lo_hz"] = f - half
        c["freq_hi_hz"] = f + half - 1
    return chans


# ---------------------------------------------------------------------------

COLS = ("freq_lo_hz", "freq_hi_hz", "freq_center_hz", "bandwidth_hz", "service",
        "label", "channel_number", "kind", "is_repeater_out", "pair_offset_hz",
        "licensed", "source", "notes")


def main(path: str) -> None:
    conn = dbmod.connect(path)
    rows = add_windows(channels()) + segments()

    conn.execute("BEGIN")
    conn.executemany(
        f"""INSERT INTO band_plan ({', '.join(COLS)})
            VALUES ({', '.join(':' + c for c in COLS)})
            ON CONFLICT (service, label, freq_lo_hz) DO UPDATE SET
              freq_hi_hz      = excluded.freq_hi_hz,
              freq_center_hz  = excluded.freq_center_hz,
              bandwidth_hz    = excluded.bandwidth_hz,
              channel_number  = excluded.channel_number,
              kind            = excluded.kind,
              is_repeater_out = excluded.is_repeater_out,
              pair_offset_hz  = excluded.pair_offset_hz,
              licensed        = excluded.licensed,
              source          = excluded.source,
              notes           = excluded.notes""",
        [{c: r.get(c) for c in COLS} for r in rows],
    )
    conn.execute("COMMIT")

    print(f"{path}: {len(rows)} band plan rows")
    for svc, kind, n, lo, hi in conn.execute(
        """SELECT service, kind, COUNT(*), MIN(freq_lo_hz), MAX(freq_hi_hz)
           FROM band_plan GROUP BY service, kind ORDER BY MIN(freq_lo_hz)"""
    ):
        print(f"  {svc:8} {kind:8} {n:3}   {lo/1e6:9.4f} – {hi/1e6:9.4f} MHz")

    # Overlapping windows within one service would make lookup ambiguous.
    bad = conn.execute(
        """SELECT a.service, a.label, b.label
           FROM band_plan a JOIN band_plan b
             ON a.service = b.service AND a.id < b.id
            AND a.kind = 'channel' AND b.kind = 'channel'
            AND a.freq_lo_hz <= b.freq_hi_hz AND b.freq_lo_hz <= a.freq_hi_hz"""
    ).fetchall()
    print(f"\n  overlapping channel windows within a service: {len(bad)}"
          f"{' — OK' if not bad else ''}")
    for r in bad[:5]:
        print(f"    {r[0]}: {r[1]} vs {r[2]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/survey.sqlite")
