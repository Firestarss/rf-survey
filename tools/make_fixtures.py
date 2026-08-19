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
        # Occupied bandwidth with realistic measurement scatter. FRS is capped at
        # 12.5 kHz; GMRS on the main channels runs up to 20 kHz. This is what the
        # service discriminator reads.
        kw.setdefault("bandwidth_hz", int(rng.gauss(11_500, 700)))
        snr = kw.pop("snr", rng.uniform(18, 45))
        ev.append(dict(receiver_id=kw.pop("rx", "uhf"),
                       t_start=t_start, t_end=t_end, duration_s=round(dur, 3),
                       freq_hz=freq, snr_db=round(snr, 1),
                       peak_dbfs=round(-88 + snr, 1), noise_dbfs=-88.0,
                       confidence=round(rng.uniform(.85, .99), 2),
                       **kw))

    # --- GMRS repeater, event production. The clean case. ------------------
    # Output 462.675 with CTCSS 141.3; input 467.675 keyed ~60 ms earlier.
    for t0, t1, d in burst(rng, T0, 22, 3600):
        lag = rng.uniform(0.04, 0.09)
        jitter = lambda: rng.randint(-400, 400)          # noqa: E731  ppm residual
        add(t0, t1, d, 462_675_000 + jitter(),
            content="voice", tone_state="ctcss", ctcss_hz=141.3,
            bandwidth_hz=int(rng.gauss(17_800, 900)))    # wideband: cannot be FRS
        add(t0 - lag, t1 - lag, d + lag, 467_675_000 + jitter(),
            content="voice", tone_state="ctcss", ctcss_hz=141.3,
            bandwidth_hz=int(rng.gauss(17_600, 900)),
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
            bandwidth_hz=int(rng.gauss(11_200, 600)))

    # --- Carrier with no content classification. Tier 1 only. --------------
    for t0, t1, d in burst(rng, T0 + 300, 5, 2000, dur=(0.3, 1.0)):
        add(t0, t1, d, 464_500_000, content=None, tone_state="unknown")

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
