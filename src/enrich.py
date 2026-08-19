#!/usr/bin/env python3
"""Turn raw detections into answers.

Four passes, each idempotent and safe to re-run:

  tag       every event gets a band_plan_id from its measured frequency
  rollup    events collapse into channels — one row per thing you could tune to
  pair      repeater input/output pairs inferred from correlated keyups
  score     each channel gets a tier: how close it is to being contactable

Everything here reads events and writes derived tables. Nothing here modifies
events except to set band_plan_id, which is a lookup result rather than an
observation. Drop `channels` and `pairs` and re-run and you get them back —
except `channels.notes`, which is yours and is preserved.

    python3 src/enrich.py data/survey.sqlite
    python3 src/enrich.py data/survey.sqlite --profile profiles/festival.yaml
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys
from collections import Counter
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bandplan  # noqa: E402

# Measured frequencies that match no band plan channel get binned to this grid
# before becoming a channel. Finer than any real channel spacing, coarse enough
# to absorb ppm error and FFT bin resolution, so one transmitter does not become
# three channel rows.
FREQ_BIN_HZ = 2_500

# A repeater retransmits what it hears, so input and output overlap in time with
# the output starting fractionally later. Anything outside this is coincidence.
PAIR_MAX_LAG_S = 1.0
PAIR_MIN_EVIDENCE = 3
PAIR_MIN_CONFIDENCE = 0.5

# Where one frequency is allotted to two services with different authorised
# bandwidths — every FRS/GMRS channel from 15 to 22 — measured bandwidth can rule
# the narrower one out. Occupied bandwidth is a property of the transmission, not
# of the path, so unlike received power it survives an unknown distance.
#
# One-sided. Wide rules out the narrow service; narrow rules out nothing, because
# a GMRS user on a narrowband radio looks exactly like an FRS user.
BW_EVIDENCE_MARGIN_HZ = 2_000   # must exceed the narrow limit by this to count
BW_MIN_EVIDENCE = 3             # measurements needed before a label is changed


# ---------------------------------------------------------------------------
# Pass 1: tag events against the band plan
# ---------------------------------------------------------------------------

def tag(conn: sqlite3.Connection, retag: bool = False) -> int:
    where = "" if retag else "WHERE band_plan_id IS NULL"
    rows = conn.execute(f"SELECT id, freq_hz FROM events {where}").fetchall()

    updates = []
    for r in rows:
        match = bandplan.best(conn, r["freq_hz"])
        updates.append((match["id"] if match else None, r["id"]))

    conn.execute("BEGIN")
    conn.executemany("UPDATE events SET band_plan_id = ? WHERE id = ?", updates)
    conn.execute("COMMIT")
    return len(updates)


# ---------------------------------------------------------------------------
# Pass 2: roll events up into channels
# ---------------------------------------------------------------------------

def _channel_key(conn: sqlite3.Connection, freq_hz: int) -> tuple[int, list]:
    """Which channel does this measurement belong to?

    A band plan channel match gives the nominal frequency — the number you would
    programme into a radio. Otherwise bin to the grid, because a ham simplex
    conversation on 146.47 is a real channel with no nominal entry.

    Returns every co-equal match, because 462.675 genuinely is both FRS 20 and
    GMRS 20 and the channel row should say so.
    """
    tied = bandplan.tied_best(conn, freq_hz)
    if tied and tied[0]["kind"] == "channel":
        return tied[0]["freq_center_hz"], tied
    return round(freq_hz / FREQ_BIN_HZ) * FREQ_BIN_HZ, tied


def _narrow_by_bandwidth(tied: list, evs: list) -> list:
    """Drop candidate services whose authorised bandwidth the signal exceeds.

    462.675 is both FRS 20 (12.5 kHz) and GMRS 20 (20 kHz). A transmission there
    measuring 17 kHz cannot be FRS, so the FRS candidate is removed and the
    channel is labelled GMRS alone. A transmission measuring 11 kHz eliminates
    nothing — narrowband GMRS radios are common and look identical to FRS.

    Returns `tied` unchanged when there is no disagreement to settle, when the
    candidates share a bandwidth limit, or when too few events carry a usable
    measurement.
    """
    if len(tied) < 2:
        return tied
    limits = {t["bandwidth_hz"] for t in tied if t["bandwidth_hz"]}
    if len(limits) < 2:
        return tied

    widths = sorted(e["bandwidth_hz"] for e in evs if e["bandwidth_hz"])
    if len(widths) < BW_MIN_EVIDENCE:
        return tied
    measured = statistics.median(widths)

    kept = [t for t in tied
            if not t["bandwidth_hz"]
            or measured <= t["bandwidth_hz"] + BW_EVIDENCE_MARGIN_HZ]
    return kept or tied


def _modal(values: list) -> tuple[object | None, float]:
    """Most common non-null value and what fraction of the total it represents.

    Ties break by string representation, not by set iteration order — Python
    randomises string hashing per process, so max(set(...)) would give a
    different answer on different runs of identical data.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return None, 0.0
    counts = Counter(vals)
    top = min(counts, key=lambda v: (-counts[v], str(v)))
    return top, counts[top] / len(vals)


def rollup(conn: sqlite3.Connection, min_agreement: float = 0.8) -> int:
    """Rebuild the channels table from events. Hand-written notes survive.

    `min_agreement` is deliberately strict. At 0.6, a shared FRS channel where a
    third of traffic runs CTCSS and two thirds does not gets recorded as "no tone"
    and promoted to tier 4 — which would have you programme a radio that the
    tone-squelched third never hears. Below the threshold the channel is marked
    unknown, which is the honest answer for a frequency several groups share.
    """
    notes = dict(conn.execute(
        "SELECT freq_hz, notes FROM channels WHERE notes IS NOT NULL"))

    buckets: dict[int, list[sqlite3.Row]] = {}
    plans: dict[int, list] = {}
    for e in conn.execute("SELECT * FROM events"):
        key, tied = _channel_key(conn, e["freq_hz"])
        buckets.setdefault(key, []).append(e)
        plans.setdefault(key, tied)

    now = time.time()
    rows = []
    for freq, evs in buckets.items():
        tied = _narrow_by_bandwidth(plans[freq], evs)
        plan = tied[0] if tied else None
        # A shared allocation gets both names, so the report does not silently
        # claim a GMRS repeater output is an FRS channel.
        label = " / ".join(dict.fromkeys(t["label"] for t in tied)) or None

        modulation, _ = _modal([e["modulation"] for e in evs])
        content, _ = _modal([e["content"] for e in evs])

        # Tone is the one field where disagreement matters. A repeater with a
        # consistent CTCSS gives the same value every keyup; a shared simplex
        # frequency with several users gives a mix. Below the agreement floor,
        # record that it is contested rather than picking the plurality.
        states = [e["tone_state"] for e in evs if e["tone_state"] != "unknown"]
        tone_state, agree = _modal(states)
        ctcss = dcs = pol = None
        if tone_state is None:
            tone_state = "unknown"
        elif agree < min_agreement:
            tone_state = "unknown"
        elif tone_state == "ctcss":
            ctcss, a2 = _modal([e["ctcss_hz"] for e in evs])
            if a2 < min_agreement:
                ctcss, tone_state = None, "unknown"
        elif tone_state == "dcs":
            dcs, a2 = _modal([e["dcs_code"] for e in evs])
            pol, _ = _modal([e["dcs_polarity"] for e in evs])
            if a2 < min_agreement:
                dcs, pol, tone_state = None, None, "unknown"

        rows.append(dict(
            freq_hz=freq,
            service=plan["service"] if plan else None,
            label=label,
            band_plan_id=plan["id"] if plan else None,
            modulation=modulation,
            content=content,
            tone_state=tone_state,
            ctcss_hz=ctcss,
            dcs_code=dcs,
            dcs_polarity=pol,
            first_seen=min(e["t_start"] for e in evs),
            last_seen=max(e["t_start"] for e in evs),
            event_count=len(evs),
            total_airtime_s=sum(e["duration_s"] for e in evs),
            rebuilt_at=now,
            notes=notes.get(freq),
        ))

    conn.execute("BEGIN")
    conn.execute("DELETE FROM channels")
    conn.executemany(
        """INSERT INTO channels
           (freq_hz, service, label, band_plan_id, modulation, content, tone_state,
            ctcss_hz, dcs_code, dcs_polarity, first_seen, last_seen, event_count,
            total_airtime_s, rebuilt_at, notes)
           VALUES (:freq_hz,:service,:label,:band_plan_id,:modulation,:content,
                   :tone_state,:ctcss_hz,:dcs_code,:dcs_polarity,:first_seen,
                   :last_seen,:event_count,:total_airtime_s,:rebuilt_at,:notes)""",
        rows)
    # Re-point events at their channel now that ids exist.
    conn.execute("UPDATE events SET channel_id = NULL")
    conn.execute("COMMIT")

    ids = dict(conn.execute("SELECT freq_hz, id FROM channels"))
    links = []
    for freq, evs in buckets.items():
        links += [(ids[freq], e["id"]) for e in evs]
    conn.execute("BEGIN")
    conn.executemany("UPDATE events SET channel_id = ? WHERE id = ?", links)
    conn.execute("COMMIT")
    return len(rows)


# ---------------------------------------------------------------------------
# Pass 3: infer repeater pairs
# ---------------------------------------------------------------------------

def pair(conn: sqlite3.Connection) -> int:
    """Match repeater outputs to their inputs using overlapping keyups.

    Frequency offset alone is not evidence — 462.675 and 467.675 are 5 MHz apart
    whether or not a repeater links them. What makes it a pair is that keying the
    input reliably produces output at the same instant.
    """
    conn.execute("BEGIN")
    conn.execute("DELETE FROM pairs")
    conn.execute("UPDATE channels SET pair_id = NULL")
    conn.execute("COMMIT")

    # Candidate offsets come from the band plan, not from a hardcoded list.
    offsets = {r[0] for r in conn.execute(
        "SELECT DISTINCT pair_offset_hz FROM band_plan WHERE pair_offset_hz IS NOT NULL")}
    if not offsets:
        return 0

    chans = {c["freq_hz"]: c for c in conn.execute("SELECT * FROM channels")}
    events: dict[int, list[sqlite3.Row]] = {}
    for e in conn.execute("SELECT * FROM events WHERE channel_id IS NOT NULL"):
        events.setdefault(e["channel_id"], []).append(e)

    found = []
    for f_out, out_ch in chans.items():
        for off in offsets:
            in_ch = chans.get(f_out + off)
            if not in_ch:
                continue
            outs = events.get(out_ch["id"], [])
            ins = sorted(events.get(in_ch["id"], []), key=lambda e: e["t_start"])
            if len(outs) < PAIR_MIN_EVIDENCE:
                continue

            lags = []
            for o in outs:
                best = None
                for i in ins:
                    if i["t_start"] > o["t_end"] + PAIR_MAX_LAG_S:
                        break
                    lag = o["t_start"] - i["t_start"]
                    if abs(lag) <= PAIR_MAX_LAG_S:
                        if best is None or abs(lag) < abs(best):
                            best = lag
                if best is not None:
                    lags.append(best)

            conf = len(lags) / len(outs)
            if len(lags) >= PAIR_MIN_EVIDENCE and conf >= PAIR_MIN_CONFIDENCE:
                found.append((out_ch["id"], in_ch["id"], off, len(lags),
                              statistics.median(lags), round(conf, 3), time.time()))

    conn.execute("BEGIN")
    conn.executemany(
        """INSERT INTO pairs (output_channel_id, input_channel_id, offset_hz,
                              evidence_count, median_lag_s, confidence, inferred_at)
           VALUES (?,?,?,?,?,?,?)""", found)
    conn.execute(
        """UPDATE channels SET pair_id =
           (SELECT id FROM pairs WHERE output_channel_id = channels.id)
           WHERE id IN (SELECT output_channel_id FROM pairs)""")
    conn.execute("COMMIT")
    return len(found)


# ---------------------------------------------------------------------------
# Pass 4: score contactability
# ---------------------------------------------------------------------------

def score(conn: sqlite3.Connection, licences: set[str] | None = None) -> dict[int, int]:
    """Assign each channel a tier 0-4.

      0  heard it — frequency and time only
      1  know the signal type — modulation identified
      2  know what it carries — voice, data or CW identified
      3  could listen properly — tone known, or confirmed absent
      4  could join in — legal for you to transmit, and if it is a repeater
         output, the input frequency is known

    Tier 4 depends on what you hold, so `licences` comes from the profile:
    a set of service names you may transmit on, e.g. {'ham2m','ham70cm','frs'}.
    Unlicensed services are always included. With no profile, tier caps at 3.
    """
    licences = set(licences or ())

    tiers = {}
    for c in conn.execute("""SELECT c.*, b.licensed, b.is_repeater_out, b.kind
                             FROM channels c
                             LEFT JOIN band_plan b ON b.id = c.band_plan_id"""):
        t = 0
        if c["modulation"]:
            t = 1
            if c["content"]:
                t = 2
                if c["tone_state"] in ("none", "ctcss", "dcs"):
                    t = 3
                    may_transmit = (
                        c["service"] is not None
                        and (c["licensed"] == 0 or c["service"] in licences)
                    )
                    repeater_ok = not c["is_repeater_out"] or c["pair_id"] is not None
                    if may_transmit and repeater_ok:
                        t = 4
        tiers[c["id"]] = t

    conn.execute("BEGIN")
    conn.executemany("UPDATE channels SET tier = ? WHERE id = ?",
                     [(t, i) for i, t in tiers.items()])
    conn.execute("COMMIT")
    return tiers


# ---------------------------------------------------------------------------

def run_all(conn, licences=None, verbose=True):
    n_tag = tag(conn)
    n_ch = rollup(conn)
    n_pair = pair(conn)
    tiers = score(conn, licences)
    if verbose:
        print(f"  tagged   {n_tag} events")
        print(f"  rolled   {n_ch} channels")
        print(f"  paired   {n_pair} repeaters")
        dist = {t: sum(1 for v in tiers.values() if v == t) for t in range(5)}
        print("  tiers    " + "  ".join(f"T{t}:{dist[t]}" for t in range(5)))
    return n_tag, n_ch, n_pair, tiers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/survey.sqlite")
    ap.add_argument("--profile", help="read operator licences from this profile")
    args = ap.parse_args()

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import db as dbmod

    licences = None
    if args.profile:
        import yaml
        prof = yaml.safe_load(open(args.profile))
        licences = set((prof.get("operator") or {}).get("licences") or ())
        print(f"  licences {sorted(licences) or 'none declared — tier caps at 3'}")

    conn = dbmod.connect(args.db)
    run_all(conn, licences)

    print()
    cur = conn.execute("SELECT * FROM v_contactable")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if not rows:
        print("  no scored channels yet")
        return
    w = [max(len(c), *(len(str(r[i] or "")) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("-" * w[i] for i in range(len(cols))))
    for r in rows:
        print("  " + "  ".join(str(r[i] if r[i] is not None else "").ljust(w[i])
                               for i in range(len(cols))))


if __name__ == "__main__":
    # Python turns SIGPIPE into an exception, so piping this into `head` raises
    # BrokenPipeError after the reader exits. Restore the default and die quietly.
    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not POSIX, or not on the main thread
    main()
