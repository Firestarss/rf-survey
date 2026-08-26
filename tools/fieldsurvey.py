#!/usr/bin/env python3
"""Turn a walk with a handheld into a propagation model.

The method: transmit from a series of surveyed points, each with a **different
CTCSS code**, so the deck's own tone decode says which transmission it was. You
do not have to note the time, keep a stopwatch, or stay in contact with anyone —
the recording identifies itself. Collect the coordinates however you like
(a phone map is fine, and was what produced the first dataset), then run `fit`.

    # while walking: the deck is just running normally
    python3 src/survey_prototype.py --driver airspy --serial <S> \
        --freq 466.0e6 --rate 10e6 --gain 42 --ppm 0.64 \
        --db data/survey.sqlite --receiver-id uhf

    # in the field, to check the deck heard you at all
    python3 tools/fieldsurvey.py list --db data/survey.sqlite --freq 462.675e6

    # afterwards, with the coordinates
    python3 tools/fieldsurvey.py fit --db data/survey.sqlite --freq 462.675e6 \
        --rx 42.3854086,-71.0796309 --points walk.csv

`walk.csv` wants a header and one row per point:

    code,lat,lon,label
    21,42.3848465,-71.0746755,corner by the school
    22,42.3868687,-71.0776927,top of the hill

Use `code` for the radio's privacy-code number, or `ctcss` for the tone in Hz if
you would rather be explicit. A point you transmitted from and the deck did not
hear still belongs in the file — those rows carry most of the information, and
`fit` reports them as misses against what the model expected.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import math
import pathlib
import sqlite3
import sys

# The radio's 38 privacy codes, in the order every handheld numbers them.
#
# NOT survey_prototype.CTCSS_TONES, which carries all 54 tones the decoder can
# identify — including 69.3, 159.8 and others that the 38-code scheme skips.
# Indexing that list by code number is off by one from code 2 upward and gets
# worse: code 21 would resolve to 131.8 instead of 136.5. Every point in a
# survey would be attributed to the wrong location, consistently enough to look
# like data.
CODE_TONES = [
    67.0, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9, 186.2,
    192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
]


def tone_for_code(code):
    if not 1 <= code <= len(CODE_TONES):
        raise SystemExit(f"privacy code {code} is outside 1-{len(CODE_TONES)}")
    return CODE_TONES[code - 1]


def code_for_tone(hz):
    for i, t in enumerate(CODE_TONES, 1):
        if abs(t - hz) < 0.05:
            return i
    return None


# A transmission is identified by whichever subaudible signalling it carried.
# Radios offer both and number them in one continuous list, so a walk can mix
# them freely — which is why this is a key rather than a tone.

def key_of_row(row):
    """('ctcss', 136.5) or ('dcs', 23), or None if the event carried neither."""
    if row["dcs_code"] is not None:
        return ("dcs", int(row["dcs_code"]))
    if row["ctcss_hz"] is not None:
        return ("ctcss", round(float(row["ctcss_hz"]), 1))
    return None


def describe(key, polarity=None):
    if key is None:
        return "--"
    kind, val = key
    if kind == "dcs":
        return f"DCS {val:03d}{polarity or ''}"
    code = code_for_tone(val)
    return f"CTCSS {val:.1f}" + (f" (code {code})" if code else "")


# --- geometry --------------------------------------------------------------

EARTH_M = 6371000.0


def distance_m(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


def bearing_deg(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


# --- database --------------------------------------------------------------

def open_db(path):
    if not pathlib.Path(path).is_file():
        raise SystemExit(f"no such database: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def heard(conn, freq_hz, since_min=None):
    """Every analysed event on one channel, strongest first per tone."""
    sql = ["SELECT freq_hz, t_start, duration_s, snr_db, ctcss_hz, confidence,"
           "       deviation_hz, harmonic_of, dcs_code, dcs_polarity"
           "  FROM events WHERE (ctcss_hz IS NOT NULL OR dcs_code IS NOT NULL)"]
    args = []
    if freq_hz is not None:
        sql.append("AND freq_hz = ?")
        args.append(int(round(freq_hz)))
    if since_min:
        sql.append("AND t_start > (SELECT MAX(t_start) FROM events) - ?")
        args.append(since_min * 60.0)
    sql.append("ORDER BY t_start")
    return conn.execute(" ".join(sql), args).fetchall()


# --- commands --------------------------------------------------------------

def cmd_list(args):
    conn = open_db(args.db)
    rows = heard(conn, args.freq, args.since)
    if not rows:
        print("nothing with a decoded tone" +
              (f" on {args.freq/1e6:.4f} MHz" if args.freq else ""))
        print("\nIf you were transmitting, the deck did not hear you. That is a")
        print("result, not a fault — note the location and keep walking.")
        return 0
    print(f"{'time':>10} {'MHz':>11} {'signalling':>20} "
          f"{'dur':>7} {'SNR':>7} {'cap':>5}")
    print("-" * 68)
    for r in rows:
        ts = datetime.datetime.fromtimestamp(
            r["t_start"], datetime.timezone.utc).strftime("%H:%M:%S")
        flag = "  (receiver product)" if r["harmonic_of"] else ""
        print(f"{ts:>10} {r['freq_hz']/1e6:11.4f} "
              f"{describe(key_of_row(r), r['dcs_polarity']):>20} "
              f"{r['duration_s'] or 0:6.2f}s {r['snr_db'] or 0:6.1f} "
              f"{r['confidence'] or 0:5.2f}{flag}")
    print(f"\n{len(rows)} transmissions identified by tone.")
    return 0


def read_points(path):
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in row.items()}
            if row.get("dcs"):
                key = ("dcs", int(str(row["dcs"]).lstrip("0") or "0"))
                shown = f"DCS {key[1]:03d}"
            elif row.get("ctcss"):
                key = ("ctcss", round(float(row["ctcss"]), 1))
                shown = describe(key)
            elif row.get("code"):
                key = ("ctcss", round(tone_for_code(int(row["code"])), 1))
                shown = describe(key)
            else:
                raise SystemExit(
                    "points file needs a 'code', 'ctcss' or 'dcs' column")
            out.append(dict(key=key, shown=shown,
                            lat=float(row["lat"]), lon=float(row["lon"]),
                            label=row.get("label", "")))
    if not out:
        raise SystemExit("points file is empty")
    return out


def fit_line(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        raise SystemExit("all points are the same distance — nothing to fit")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    inter = my - slope * mx
    ss_res = sum((y - (inter + slope * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, inter, r2, math.sqrt(ss_res / n)


def cmd_fit(args):
    conn = open_db(args.db)
    rx = tuple(float(v) for v in args.rx.split(","))
    if len(rx) != 2:
        raise SystemExit("--rx wants LAT,LON")
    points = read_points(args.points)

    rows = heard(conn, args.freq, args.since)
    best = {}
    for r in rows:
        if r["harmonic_of"]:
            continue           # a receiver product is not a propagation sample
        k = key_of_row(r)
        if k is None or r["snr_db"] is None:
            continue
        if r["snr_db"] > best.get(k, (-999,))[0]:
            best[k] = (r["snr_db"], r)

    for p in points:
        got = best.get(p["key"])
        p["snr"] = got[0] if got else None
        p["dist"] = distance_m(rx, (p["lat"], p["lon"]))
        p["brg"] = bearing_deg(rx, (p["lat"], p["lon"]))

    got = [p for p in points if p["snr"] is not None]
    if len(got) < 3:
        raise SystemExit(f"only {len(got)} points were heard — need 3 to fit")

    slope, inter, r2, sigma = fit_line(
        [math.log10(p["dist"]) for p in got], [p["snr"] for p in got])

    print(f"receiver at {rx[0]:.6f}, {rx[1]:.6f}"
          + (f"   {args.freq/1e6:.4f} MHz" if args.freq else ""))
    print(f"{len(got)} of {len(points)} points heard\n")
    print(f"{'signalling':>20} {'dist':>8} {'bearing':>9} "
          f"{'SNR':>8} {'model':>8} {'resid':>8}  label")
    print("-" * 88)
    for p in sorted(points, key=lambda q: q["dist"]):
        pred = inter + slope * math.log10(p["dist"])
        brg = f"{p['brg']:5.0f} {compass(p['brg']):<2}"
        if p["snr"] is None:
            print(f"{p['shown']:>20} {p['dist']:7.0f}m "
                  f"{brg:>9} {'NOT HEARD':>8} {pred:7.1f} {'':>8}  {p['label']}")
        else:
            print(f"{p['shown']:>20} {p['dist']:7.0f}m "
                  f"{brg:>9} {p['snr']:7.1f} {pred:7.1f} "
                  f"{p['snr']-pred:+7.1f}  {p['label']}")

    print(f"\npath loss   {-slope:.1f} dB per decade of distance")
    print(f"            (free space 20, suburban 25-30, dense urban 30-40)")
    print(f"fit quality R^2 = {r2:.3f}, residual sigma {sigma:.1f} dB")
    if r2 < 0.8:
        print("            LOW — distance alone is not explaining these points.")
        print("            Look at the bearing column before trusting the range.")

    for thr in (args.on_db, args.on_db + 6.0):
        rng = 10 ** ((thr - inter) / slope)
        print(f"reaches {thr:5.1f} dB SNR at {rng:7.0f} m")
    print(f"\n(on_db in profiles/festival.yaml is the detection threshold; a"
          f"\n signal at exactly that level is a coin toss, so the second row is"
          f"\n the range you can actually rely on.)")

    misses = [p for p in points if p["snr"] is None]
    if misses:
        print("\nPoints not heard, and what the model expected there:")
        for p in sorted(misses, key=lambda q: q["dist"]):
            pred = inter + slope * math.log10(p["dist"])
            print(f"  {p['dist']:6.0f} m at {p['brg']:3.0f} deg "
                  f"{compass(p['brg']):<2} — model said {pred:5.1f} dB  {p['label']}")
        print("  A miss where the model expected a strong signal is an"
              " obstruction,\n  not a range limit. Compare its bearing against"
              " the points that worked.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Build a propagation model from a walk with a handheld.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="what the deck heard, identified by tone")
    p.add_argument("--db", required=True)
    p.add_argument("--freq", type=float, default=None,
                   help="restrict to one channel, in Hz")
    p.add_argument("--since", type=float, default=None,
                   help="only the last N minutes of the run")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("fit", help="fit path loss against surveyed points")
    p.add_argument("--db", required=True)
    p.add_argument("--freq", type=float, default=None)
    p.add_argument("--rx", required=True, help="receiver position, LAT,LON")
    p.add_argument("--points", required=True, help="CSV of code,lat,lon,label")
    p.add_argument("--since", type=float, default=None)
    p.add_argument("--on-db", dest="on_db", type=float, default=10.0,
                   help="detection threshold to project range for (default 10)")
    p.set_defaults(fn=cmd_fit)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
