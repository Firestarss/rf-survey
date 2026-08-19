"""Band plan lookup.

The detector works on continuous spectrum and reports whatever frequency it measured.
This turns that number into a label by asking which band plan ranges contain it.

Narrowest match wins, so a specific channel beats the segment it sits inside:

    146_520_000 -> 2 m national FM simplex calling   (a 20 kHz channel)
    146_470_000 -> 2 m simplex                       (a 180 kHz segment)
    145_330_000 -> 2 m FM repeater outputs           (a 300 kHz segment)
    462_674_800 -> GMRS 20 / FRS 20                  (two 12.5 kHz channels, both real)

Multiple matches at the same width are genuine ambiguity, not a bug. FRS and GMRS share
22 frequencies with different power limits and licence status; 462.675 really is both.
"""

from __future__ import annotations

import sqlite3

_LOOKUP = """
SELECT *,
       (freq_hi_hz - freq_lo_hz) AS width_hz,
       ABS(COALESCE(freq_center_hz, (freq_lo_hz + freq_hi_hz) / 2) - :f) AS offset_hz
FROM band_plan
WHERE :f BETWEEN freq_lo_hz AND freq_hi_hz
ORDER BY width_hz ASC, offset_hz ASC
"""


def lookup(conn: sqlite3.Connection, freq_hz: int) -> list[sqlite3.Row]:
    """Every band plan range containing this frequency, most specific first."""
    return conn.execute(_LOOKUP, {"f": int(freq_hz)}).fetchall()


def best(conn: sqlite3.Connection, freq_hz: int) -> sqlite3.Row | None:
    """The single most specific match, or None if the frequency is unallocated."""
    rows = lookup(conn, freq_hz)
    return rows[0] if rows else None


def describe(conn: sqlite3.Connection, freq_hz: int) -> str:
    """One-line human summary. Names co-equal matches rather than picking one."""
    rows = lookup(conn, freq_hz)
    if not rows:
        return f"{freq_hz/1e6:.4f} MHz — not in the band plan"

    top = rows[0]["width_hz"]
    tied = [r for r in rows if r["width_hz"] == top]
    names = " / ".join(f"{r['label']} ({r['service']})" for r in tied)

    out = f"{freq_hz/1e6:.4f} MHz — {names}"
    if tied[0]["kind"] == "channel":
        off = tied[0]["offset_hz"]
        out += f", {off/1000:+.2f} kHz from nominal"
    wider = [r for r in rows if r["width_hz"] > top]
    if wider:
        out += f"; within {wider[-1]['label']}"
    return out


def unallocated(conn: sqlite3.Connection, freqs: list[int]) -> list[int]:
    """Which of these frequencies no band plan row covers. Useful after a survey."""
    return [f for f in freqs if not lookup(conn, f)]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    import db as dbmod

    path = sys.argv[1] if len(sys.argv) > 1 else "data/survey.sqlite"
    conn = dbmod.connect(path, readonly=True)

    print("Layered resolution — same table, specific beats general:\n")
    for f in (
        146_520_000,      # exact national simplex calling
        146_519_400,      # 600 Hz off, should still snap to it
        146_470_000,      # simplex segment, no specific channel
        145_330_000,      # repeater output segment
        144_390_000,      # APRS
        446_000_000,      # 70 cm national simplex
        442_500_000,      # 70 cm repeater segment
        462_674_800,      # GMRS 20 / FRS 20, measured 200 Hz low
        467_675_000,      # GMRS 20 repeater input
        154_570_000,      # MURS, ex Blue Dot
        467_850_000,      # Silver Star
        451_800_000,      # itinerant, outside every scan window
        433_920_000,      # ISM — deliberately unallocated here
    ):
        print("  " + describe(conn, f))
