#!/usr/bin/env python3
"""Generate a synthetic festival scenario.

Fake events with realistic structure, so the enricher can be developed and
regression-tested before any radio exists. Deterministic — same seed, same rows.

Deliberately includes the awkward cases:
  - a GMRS repeater whose input keyups precede its output by ~60 ms
  - a shared FRS channel where three groups use three different tones
  - a channel where the tone was never checked
  - a signal on an unallocated frequency
  - a 2 m simplex conversation with no band plan channel entry
  - a near-miss pair at the right offset with uncorrelated timing, which must NOT
    be reported as a repeater
  - transmissions shorter than the analysis dwell, which are detected but never
    examined, and must stay at tier 0
  - a distant handheld whose weak keyups measure wide and whose strong ones do
    not, which is mislabelled without the SNR gate on deviation evidence
  - DCS suspected but never decoded, which must cap at tier 2
  - an in-flight event with no t_end, the row a power cut leaves behind

    python3 tools/make_fixtures.py data/survey.sqlite
    python3 tools/make_fixtures.py data/survey.sqlite --wipe
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import db as dbmod  # noqa: E402
from survey_prototype import ANALYZE_SECONDS  # noqa: E402

T0 = 1_756_000_000.0  # fixed epoch so runs are comparable
SEED = 20260818


def burst(rng, base, n, spread, dur=(1.5, 8.0)):
    """n transmission start/end pairs scattered over `spread` seconds."""
    out = []
    for _ in range(n):
        t = base + rng.uniform(0, spread)
        d = rng.uniform(*dur)
        out.append((t, t + d, d))
    return sorted(out)


def build(rng):
    ev = []

    def add(t_start, t_end, dur, freq, **kw):
        kw.setdefault("modulation", "nfm")
        kw.setdefault("tone_state", "unknown")
        # Peak FM deviation with realistic measurement scatter. FRS is capped at
        # 2.5 kHz; GMRS on the main channels runs the classic 5 kHz. This is what
        # the service discriminator reads — the deck has never measured occupied
        # bandwidth, so bandwidth_hz stays NULL exactly as it will in the field.
        kw.setdefault("deviation_hz", round(rng.gauss(2_400, 250), 1))
        snr = kw.pop("snr", rng.uniform(18, 45))

        # The detector needs ANALYZE_SECONDS of dwell before it demodulates
        # anything. Shorter keyups are detected and timed and never examined:
        # no deviation, no tone, no content. Most festival traffic is this
        # short, so this is the common case rather than an edge case, and it is
        # what puts a channel on tier 0.
        if dur is not None and dur < ANALYZE_SECONDS:
            kw.update(deviation_hz=None, tone_state="unknown", content=None,
                      ctcss_hz=None, dcs_code=None, dcs_polarity=None,
                      confidence=None)
        else:
            kw.setdefault("confidence", round(rng.uniform(.85, .99), 2))

        ev.append(dict(receiver_id=kw.pop("rx", "uhf"),
                       t_start=t_start, t_end=t_end,
                       duration_s=None if dur is None else round(dur, 3),
                       freq_hz=freq, snr_db=round(snr, 1),
                       peak_dbfs=round(-88 + snr, 1), noise_dbfs=-88.0,
                       **kw))

    # --- GMRS repeater, event production. The clean case. ------------------
    # Output 462.675 with CTCSS 141.3; input 467.675 keyed ~60 ms earlier.
    for t0, t1, d in burst(rng, T0, 22, 3600):
        lag = rng.uniform(0.04, 0.09)
        jitter = lambda: rng.randint(-400, 400)          # noqa: E731  ppm residual
        add(t0, t1, d, 462_675_000 + jitter(),
            content="voice", tone_state="ctcss", ctcss_hz=141.3,
            deviation_hz=round(rng.gauss(4_900, 300), 1))  # wide: cannot be FRS
        add(t0 - lag, t1 - lag, d + lag, 467_675_000 + jitter(),
            content="voice", tone_state="ctcss", ctcss_hz=141.3,
            deviation_hz=round(rng.gauss(4_800, 300), 1),
            snr=rng.uniform(8, 22))                      # input heard weaker

    # --- FRS 1, vendors. Three groups, three tones, no agreement. ----------
    for t0, t1, d in burst(rng, T0 + 200, 18, 3400, dur=(0.8, 3.0)):
        tone = rng.choice([("ctcss", 67.0), ("ctcss", 100.0), ("none", None)])
        add(t0, t1, d, 462_562_500 + rng.randint(-300, 300),
            content="voice", tone_state=tone[0], ctcss_hz=tone[1])

    # --- MURS 154.570, security. DCS, consistent. --------------------------
    for t0, t1, d in burst(rng, T0 + 500, 14, 3200, dur=(1.0, 5.0)):
        add(t0, t1, d, 154_570_000 + rng.randint(-200, 200), rx="vhf",
            content="voice", tone_state="dcs", dcs_code=155, dcs_polarity="N")

    # --- Part 90 Silver Star, stage crew. Tone never checked. --------------
    for t0, t1, d in burst(rng, T0 + 100, 9, 3500, dur=(0.5, 2.5)):
        add(t0, t1, d, 467_850_000 + rng.randint(-250, 250), content="voice")

    # --- 2 m ham repeater output, no tone. ---------------------------------
    for t0, t1, d in burst(rng, T0 + 800, 11, 3000, dur=(4.0, 30.0)):
        add(t0, t1, d, 146_820_000 + rng.randint(-150, 150), rx="vhf",
            content="voice", tone_state="none")

    # --- 2 m simplex conversation. No band plan channel, only a segment. ---
    for t0, t1, d in burst(rng, T0 + 1500, 7, 900, dur=(6.0, 40.0)):
        add(t0, t1, d, 146_470_000 + rng.randint(-100, 100), rx="vhf",
            content="voice", tone_state="none")

    # --- 146.52 calling frequency, brief. ----------------------------------
    for t0, t1, d in burst(rng, T0 + 2200, 4, 1200, dur=(2.0, 6.0)):
        add(t0, t1, d, 146_520_000 + rng.randint(-600, 600), rx="vhf",
            content="voice", tone_state="none")

    # --- Data burst, unallocated frequency. --------------------------------
    for t0, t1, d in burst(rng, T0 + 60, 12, 3500, dur=(0.15, 0.5)):
        add(t0, t1, d, 463_112_500 + rng.randint(-200, 200),
            content="data", modulation="4fsk", tone_state="none")

    # --- The trap: correct 5 MHz offset, uncorrelated timing. --------------
    # 462.7000 (GMRS 21) and 467.7000. Both busy, never at the same moment.
    # Offset alone must not be enough to call this a repeater pair.
    for t0, t1, d in burst(rng, T0, 10, 3600, dur=(2.0, 6.0)):
        add(t0, t1, d, 462_700_000, content="voice", tone_state="none")
    for t0, t1, d in burst(rng, T0 + 1_800, 10, 1500, dur=(2.0, 6.0)):
        add(t0 + 90, t1 + 90, d, 467_700_000, content="voice", tone_state="none")

    # --- Strong but narrowband on a main channel. The control case. --------
    # Loud enough that a power-based rule would call it GMRS. Bandwidth says it
    # could be either, so the label must stay shared.
    for t0, t1, d in burst(rng, T0 + 400, 8, 3000, dur=(1.0, 4.0)):
        add(t0, t1, d, 462_650_000 + rng.randint(-300, 300),
            content="voice", tone_state="none", snr=rng.uniform(52, 60),
            deviation_hz=round(rng.gauss(2_350, 200), 1))

    # --- Brief keyups, never analysed. Tier 0. -----------------------------
    # Every transmission here is shorter than the analysis dwell, so the deck
    # detects them and learns nothing else about them. Tier 0 has never had a
    # fixture before, which meant the bottom rung of the ladder was untested.
    #
    # The upper bound tracks ANALYZE_SECONDS rather than being written out. It
    # was hardcoded at 1.0 s when the dwell was 1.4 s; the dwell later dropped
    # to 0.9 s and this channel quietly started arriving at tier 1, still
    # described everywhere as the tier 0 case.
    for t0, t1, d in burst(rng, T0 + 300, 5, 2000,
                           dur=(0.3, ANALYZE_SECONDS - 0.05)):
        add(t0, t1, d, 464_500_000, content=None, tone_state="unknown")

    # --- The distant handheld. FRS 22 / GMRS 22, mostly heard badly. -------
    # The regression case for the SNR gate. This is one narrowband FRS radio at
    # the far end of the site: its few close keyups measure 2.4 kHz, and its
    # many distant ones measure over 5 kHz because FM clicks inflate the p99
    # estimator below the demodulation threshold.
    #
    # Ungated, the median of all twelve is a wideband number and the FRS
    # candidate is struck — the deck would tell you a handheld you are licensed
    # to talk to on FRS is GMRS-only. Gated at 18 dB, only the four good
    # measurements count and the shared label survives.
    for t0, t1, d in burst(rng, T0 + 700, 8, 3000, dur=(1.5, 4.0)):
        add(t0, t1, d, 462_725_000 + rng.randint(-300, 300),
            content="voice", tone_state="none", snr=rng.uniform(9, 15),
            deviation_hz=round(rng.gauss(5_400, 400), 1))   # inflated by clicks
    for t0, t1, d in burst(rng, T0 + 2400, 4, 800, dur=(1.5, 4.0)):
        add(t0, t1, d, 462_725_000 + rng.randint(-300, 300),
            content="voice", tone_state="none", snr=rng.uniform(26, 38),
            deviation_hz=round(rng.gauss(2_400, 200), 1))   # heard properly

    # --- DCS suspected, never decoded. Must cap at tier 2. -----------------
    # GMRS 18, a service the operator is licensed for, heard loudly, with a
    # consistent subaudible signal that is clearly not CTCSS. Everything else
    # about it says tier 4 — the missing codeword is the only thing stopping
    # it, and "probably some DCS" will not programme a radio.
    for t0, t1, d in burst(rng, T0 + 900, 9, 3000, dur=(2.0, 6.0)):
        add(t0, t1, d, 462_625_000 + rng.randint(-200, 200),
            content="voice", tone_state="dcs", dcs_code=None,
            snr=rng.uniform(30, 44))

    # --- In flight. The row a power cut leaves behind. ---------------------
    # Detected and analysed, never closed: t_end and duration_s stay NULL. The
    # rollup must count it as evidence the channel was busy without claiming to
    # know for how long, and must not crash summing a NULL.
    add(T0 + 3595, None, None, 462_675_000,
        content="voice", tone_state="ctcss", ctcss_hz=141.3,
        deviation_hz=round(rng.gauss(4_900, 300), 1))

    return sorted(ev, key=lambda e: e["t_start"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/survey.sqlite")
    ap.add_argument("--wipe", action="store_true",
                    help="delete existing runs and events first")
    ap.add_argument("--profile", default="profiles/festival.yaml")
    args = ap.parse_args()

    conn = dbmod.connect(args.db)
    if args.wipe:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM runs")        # cascades to events and decodes
        conn.execute("DELETE FROM channels")
        conn.execute("DELETE FROM pairs")
        conn.execute("COMMIT")

    run = dbmod.start_run(conn, args.profile, notes="SYNTHETIC — not real signals")
    dbmod.register_receiver(conn, run, "uhf", "SYNTHETIC-UHF", 10_000_000,
                            gain_db=12, ppm_error=0.0, attenuator_db=20)
    dbmod.register_receiver(conn, run, "vhf", "SYNTHETIC-VHF", 10_000_000,
                            gain_db=12, ppm_error=0.0, attenuator_db=20)

    rng = random.Random(SEED)
    events = build(rng)
    for e in events:
        rx = e.pop("receiver_id")
        dbmod.log_event(conn, run, rx, **{k: v for k, v in e.items() if v is not None})
    dbmod.end_run(conn, run)

    print(f"{args.db}: run {run}, {len(events)} synthetic events")
    for f, n in conn.execute(
        """SELECT freq_hz/12500*12500 AS f, COUNT(*) FROM events
           GROUP BY f ORDER BY f"""):
        print(f"  {f/1e6:9.4f} MHz   {n:3} events")


if __name__ == "__main__":
    # Python turns SIGPIPE into an exception, so piping this into `head` raises
    # BrokenPipeError after the reader exits. Restore the default and die quietly.
    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not POSIX, or not on the main thread
    main()
