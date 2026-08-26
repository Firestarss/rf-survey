#!/usr/bin/env python3
"""Work out how much attenuation this receiver wants, on this antenna, here.

The number is not a property of the radio. It depends on how much man-made noise
is arriving, which changes with the band, the antenna and the site — Phase 1
measured 4-6 dB at 466 MHz and 17-19 dB at 146 MHz on the same afternoon with
the same hardware. So it has to be measured wherever the deck is going to stand,
and this does that without needing anything but the radio, an antenna and a
dummy load.

    python3 tools/padcal.py --serial <SERIAL> --freq 466.0e6 --pad 20

`--pad` is what is physically fitted **right now**, in dB. The tool measures how
far the antenna lifts the noise floor above what the receiver makes on its own,
and works back to the attenuation that would put that lift in the 8-10 dB window
where external noise dominates without wasting headroom.

WHAT IT ASKS YOU TO DO

  1. fit the ANTENNA, press enter
  2. swap it for the DUMMY LOAD, press enter

That is the whole procedure. Everything else is arithmetic.

WHY IT SWEEPS SEVERAL GAINS

Two failure modes sit either side of the useful range and both are silent.

Below the ADC knee the converter's own noise swamps everything, the floor stops
responding to gain at all, and the antenna-versus-dummy delta collapses to zero
no matter what is in the air. Above the compression point the front end runs out
of headroom, the floor stops rising *again*, and everything the deck logs is
understated — with clipping counters reading zero throughout, because
compression happens well before samples reach full scale.

Both look like "the floor did not move". Sweeping gains tells them apart: in the
usable middle, equal steps of gain give equal steps of floor. The tool reports
which gains were linear and only trusts those.
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import numpy as np                                            # noqa: E402
import survey_prototype as proto                              # noqa: E402

TARGET_LO, TARGET_HI = 8.0, 10.0     # the window step 11 asks for
LINEAR_TOL = 0.7                     # a step this far under its neighbour is
                                     # compression, matching COMPRESSION_RATIO


def measure(sdr, api, grid, per, rate, seconds):
    """Median noise floor across the band, in dB."""
    fs = proto.frame_size(rate)
    buf = np.empty(fs, np.complex64)
    want = int(seconds * rate)
    got = 0
    chan = []
    while got < want:
        st = sdr.readStream(sdr._pad_stream, [buf], fs, timeoutUs=2_000_000)
        if st.ret <= 0:
            continue
        got += st.ret
        psd = per(buf[:st.ret])
        if psd is None:
            continue
        chan.append(proto.to_db(grid.power(psd)).astype(np.float32))
    if not chan:
        raise SystemExit("no samples — is the radio still connected?")
    return float(np.median(proto.spectrum_floor(np.asarray(chan))))


def sweep(sdr, api, grid, per, rate, gains, seconds, label):
    print(f"\n  measuring with the {label}...")
    out = {}
    for g in gains:
        sdr.setGain(api.SOAPY_SDR_RX, 0, float(g))
        time.sleep(0.15)                      # let the stage settle
        out[g] = measure(sdr, api, grid, per, rate, seconds)
        print(f"    gain {g:5.1f} -> floor {out[g]:8.1f} dB")
    return out


def linear_gains(floors, gains):
    """Gains where the floor is genuinely tracking, as (gain, verdict) pairs."""
    verdicts = {}
    for i, g in enumerate(gains):
        if i < 2:
            verdicts[g] = "edge"          # no pair below it to compare against
            continue
        lo, mid, hi = (floors[gains[i - 2]], floors[gains[i - 1]], floors[g])
        verdicts[g] = proto.compression_verdict([lo, mid, hi])
    return verdicts


def main():
    ap = argparse.ArgumentParser(
        description="Measure the attenuation this site and antenna want.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--serial", default=None)
    ap.add_argument("--driver", default="airspy")
    ap.add_argument("--freq", type=float, required=True, help="centre, Hz")
    ap.add_argument("--rate", type=float, default=10e6)
    ap.add_argument("--ppm", type=float, default=0.0)
    ap.add_argument("--pad", type=float, required=True,
                    help="attenuation physically fitted right now, in dB")
    ap.add_argument("--gains", default="30,33,36,39,42,45")
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="per gain, per load (default 4)")
    args = ap.parse_args()

    gains = [float(x) for x in args.gains.split(",")]
    import SoapySDR
    sdr = SoapySDR.Device(proto.device_args(args.driver, args.serial))
    RX = SoapySDR.SOAPY_SDR_RX
    sdr.setSampleRate(RX, 0, args.rate)
    sdr.setFrequency(RX, 0, proto.tune_request_hz(args.freq, args.ppm))
    try:
        sdr.setGainMode(RX, 0, False)
    except Exception:
        print("  warning: could not disable AGC", file=sys.stderr)
    rate = sdr.getSampleRate(RX, 0)
    centre = proto.true_center_hz(sdr.getFrequency(RX, 0), args.ppm)
    grid = proto.ChannelGrid(centre, rate)
    per = proto.Periodogram(rate)

    print(f"padcal — {centre/1e6:.4f} MHz, {rate/1e6:.1f} MSPS, "
          f"{args.pad:.0f} dB fitted")
    print(f"gains: {', '.join(f'{g:g}' for g in gains)}   "
          f"{args.seconds:.0f} s each, both loads")
    print(f"about {len(gains)*args.seconds*2/60:.1f} minutes of measuring")

    sdr._pad_stream = sdr.setupStream(RX, SoapySDR.SOAPY_SDR_CF32)
    sdr.activateStream(sdr._pad_stream)
    try:
        input("\n  Fit the ANTENNA, then press enter... ")
        ant = sweep(sdr, SoapySDR, grid, per, rate, gains, args.seconds, "antenna")
        input("\n  Now fit the DUMMY LOAD, then press enter... ")
        dum = sweep(sdr, SoapySDR, grid, per, rate, gains, args.seconds, "dummy load")
    finally:
        sdr.deactivateStream(sdr._pad_stream)
        sdr.closeStream(sdr._pad_stream)

    ant_ok = linear_gains(ant, gains)
    dum_ok = linear_gains(dum, gains)

    print(f"\n  {'gain':>6} {'antenna':>9} {'dummy':>9} {'delta':>8}  state")
    print("  " + "-" * 52)
    usable = []
    for g in gains:
        d = ant[g] - dum[g]
        state = ("compressed" if "compressed" in (ant_ok[g], dum_ok[g])
                 else "edge" if "edge" in (ant_ok[g], dum_ok[g])
                 else "inconclusive" if "inconclusive" in (ant_ok[g], dum_ok[g])
                 else "linear")
        if state == "linear":
            usable.append((g, d))
        print(f"  {g:6.1f} {ant[g]:9.1f} {dum[g]:9.1f} {d:+8.1f}  {state}")

    if not usable:
        print("\n  No gain was both above the converter's noise and below")
        print("  compression. Widen --gains, or the signal environment is too")
        print("  strong for any setting with this much attenuation fitted.")
        return 1

    # Above the knee the ratio is fixed, so every usable gain should agree; a
    # spread means something moved during the measurement.
    ratios = [(10 ** (d / 10.0) - 1.0) * 10 ** (args.pad / 10.0)
              for _, d in usable]
    r0 = sum(ratios) / len(ratios)
    deltas = [d for _, d in usable]
    print(f"\n  usable gains: {', '.join(f'{g:g}' for g, _ in usable)}")
    print(f"  delta {min(deltas):+.1f} to {max(deltas):+.1f} dB"
          f"  (should be flat — it does not depend on gain above the knee)")
    if max(deltas) - min(deltas) > 2.0:
        print("  SPREAD IS HIGH. Something changed while measuring — a burst of"
              "\n  traffic, or the antenna moved. Re-run before trusting it.")
    print(f"  external noise is {r0:.1f}x the receiver's own, before attenuation")

    print(f"\n  {'pad':>6} {'predicted delta':>17}")
    best = None
    for pad10 in range(0, 401):
        pad = pad10 / 10.0
        d = 10 * math.log10(1.0 + r0 * 10 ** (-pad / 10.0))
        if TARGET_LO <= d <= TARGET_HI and best is None:
            best = (pad, d)
        if pad == int(pad) and (pad <= 12 or pad % 5 == 0):
            mark = "  <-- in the 8-10 window" if TARGET_LO <= d <= TARGET_HI else ""
            print(f"  {pad:5.0f} dB {d:14.1f} dB{mark}")
    if best is None:
        print("\n  Nothing in 0-40 dB reaches the window. With this little"
              "\n  ambient noise the receiver's own noise dominates whatever you"
              "\n  do; fit as little attenuation as the strong-signal case allows.")
        return 1

    hi = None
    for pad10 in range(400, -1, -1):
        pad = pad10 / 10.0
        d = 10 * math.log10(1.0 + r0 * 10 ** (-pad / 10.0))
        if TARGET_LO <= d <= TARGET_HI:
            hi = pad
            break
    print(f"\n  FIT {hi:.0f} dB  (anything from {best[0]:.0f} to {hi:.0f} dB"
          f" satisfies the rule)")
    print(f"  Take the largest of them. They are equivalent for noise, and the")
    print(f"  bigger pad keeps more headroom for the case the rule ignores —")
    print(f"  somebody keying a handheld a few metres from the antenna.")
    print(f"\n  Currently fitted: {args.pad:.0f} dB"
          + ("  (already right)" if abs(args.pad - hi) < 1.5
             else f"  -> change to {hi:.0f} dB"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
