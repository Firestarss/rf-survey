# Phase log

Running record of gate results. One entry per phase, appended as it closes.
Full procedures live in `docs/bench-bringup.md`.

| Phase | Status | Date | Headline |
|---|---|---|---|
| **0** — Pi alone, no radio | **PASS** | 2026-08-18 | 28.8% of one core, 27 concurrent, peak 71.6 °C, zero throttling, fan confirmed |
| **1** — first radio, first signal | in progress | 2026-08-26 | radio 1 up; 7/7 FRS channels, −0.644 ppm; MiniSA and antenna steps outstanding |
| **2** — RF front end | — | — | |
| **3** — tones and classification | — | — | |
| **4** — logging and database | — | — | |
| **5** — second receiver | — | — | |
| **6** — dual-bus USB load | — | — | |
| **7** — repeater pairing | — | — | |
| **8** — 24 h unattended soak | — | — | |

---

## Phase 0 — 2026-08-18 — PASS

**Machine as built:** `radio-deck`, Pi 5 8 GB (CanaKit Essentials), GeeekPi metal case with
official M.2 HAT+ and Active Cooler, WD Black SN770M 500 GB NVMe, Ubuntu 26.04 LTS,
kernel 7.0.0-1016-raspi, booting NVMe with `BOOT_ORDER=0xf146`.

**Compute headroom is roughly double the estimate.** `--selftest` held 28.8% of one core
steady-state, which projects to about 14% of the machine for two radios. 27 simultaneous
transmissions handled. The second Airspy is no longer a question mark.

**Thermal is not a concern on the bench.** Peak 71.6 °C was `stress-ng` pinning four cores
at 100%; the real workload is 7–14%, so operating temperature sits much nearer the 44 °C
idle. Clock held 2400 MHz across all 292 samples — zero throttling.

**Cooling confirmed working.** Fan reads 0 rpm at 44 °C idle (correct — the controller stops
below ~50 °C), 529 rpm at ~61 °C on one sample and 5293 rpm on a second load run. Fan curve
is responding and has steps left. Revisit only when the enclosure is sealed.

**PCIe negotiated Gen2 x1** (5 GT/s, ~450 MB/s) against a workload needing single-digit MB/s.
Deliberately left there rather than forced to Gen3.

### Findings that change later assumptions

- **A third USB 2.0 controller exists.** `Bus 001` is a `dwc2` controller on the USB-C
  connector. If it can be put in host mode, that is a route to a third radio without a PCIe
  card — at the cost of powering the Pi through GPIO. Investigate after the first event.
- **`vcgencmd` installs but `/dev/vcio` is absent**, so `get_throttled` fails. Use
  `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq` instead — below 2400 MHz under
  load means throttling.
- **NumPy is linked against reference BLAS, not OpenBLAS.** Low priority: the hot paths are
  pocketfft and scipy's `upfirdn`, neither of which uses BLAS.
- **`eth0` is down, WiFi only.** Fine for SSH and `--spectrum`. Live remote spectrum via
  SoapyRemote at full rate is ~30 MB/s and wants ethernet; over WiFi, drop to 2.5 MSPS.
- **AppArmor logs denials against `lsusb`** on 26.04. Cosmetic — output is complete.
- **DHCP moved the machine** .243 → .244 mid-session and dropped SSH. Needs a reservation
  or mDNS; `avahi-daemon` is installed but `.local` resolution failed from Windows.

### Open items carried forward

- [ ] DHCP reservation for `radio-deck`
- [ ] WiFi power save disabled via systemd oneshot (no NetworkManager on Ubuntu Server)
- [ ] Map physical USB ports to buses and label the case — the two radios must land on
      different `480M` root hubs

---

## Phase 1 — in progress, opened 2026-08-26

Both Airspys arrived 2026-08-25. Radio 1 is on the bench; procedure in
`docs/phase1-detail.md`.

**Radio 1, as measured**

```
serial     637862dc2e4c6dd7        (airspy_info prints 0x637862DC2E4C6DD7)
usb        Bus 004 Port 1, xhci-hcd, 480M high-speed, clean enumeration
firmware   AirSpy NOS v1.0.0-rc10-0-g946184a  2016-09-19
rates      10 MSPS and 2.5 MSPS
gain       0-45 overall, filling LNA -> MIX -> VGA. NOT a 0-21 linearity control
ppm        -0.644  (mean of 7 FRS channels, spread -0.53 to -0.77)
```

**FRS channels 1-7**, dummy load, gain 42, handheld at low power a few feet away.
All seven land on the correct frequency. Spacing is even to within **72 Hz** worst
case against a nominal 25 kHz step — 0.29%, and comparable to the interpolator's own
42 Hz accuracy, so the sample-rate fault is ruled out.

| Ch | Nominal | Measured | Error | ppm |
|---|---|---|---|---|
| 1 | 462.5625 | 462.562176 | -324 Hz | -0.70 |
| 2 | 462.5875 | 462.587232 | -268 Hz | -0.58 |
| 3 | 462.6125 | 462.612256 | -244 Hz | -0.53 |
| 4 | 462.6375 | 462.637184 | -316 Hz | -0.68 |
| 5 | 462.6625 | 462.662144 | -356 Hz | -0.77 |
| 6 | 462.6875 | 462.687200 | -300 Hz | -0.65 |
| 7 | 462.7125 | 462.712224 | -276 Hz | -0.60 |

The ppm figure is the deck measured against a consumer handheld, so it is a difference
between two clocks. It is independently corroborated by a real transmitter on exactly
464.000000 MHz reading -0.6 to -0.8 ppm. Step 12 against the MiniSA is still outstanding
and is a third reference.

**Zero clipping frames** at every gain from 0 to 45 with the handheld keyed nearby.
Overflows were zero on every capture except one taken while the test suite was running
on the same Pi, which produced 344 — the deck must not share the machine with CPU-heavy
work during a survey.

**Three software faults were found by the hardware**, none of which could have been
found without it: the device could not be opened at all, `--spectrum-seconds` ran for
half its stated duration, and captures took 3.1 GB. All fixed; see handoff section 8.

**Outstanding for Gate 1**

- [ ] Notch filter measured, ≥30 dB at 88-108 and ≤1.5 dB at 466 (MiniSA, step 9)
- [ ] Both 20 dB pads measured and labelled A/B, difference recorded (MiniSA, step 10)
- [ ] Antenna-versus-dummy delta 8-10 dB — start at gain 36, not 12 (step 11)
- [ ] ppm confirmed against the MiniSA generator (step 12)
- [ ] Everything in the spectrum accounted for (step 13)

**Open question carried into the gate.** Three signals sit above `detection.on_db`
of 10.0 dB with a dummy load fitted. 470.000000 MHz reports exactly 0 Hz offset and is
internal — a spur generated from the deck's own reference is coherent with it, so it
shows no clock error, while a real signal shows the receiver's. 464.000000 MHz and the
wandering 468.1/468.5 signals show the deck's -0.64 ppm and are real Boston traffic
leaking past the dummy load. The internal one would be logged as traffic on a channel
nobody keyed.

**The deck records its own evidence for the serial and the ppm.** The serial is read
back off the device rather than trusted from the command line, and it was confirmed
against this radio: `getHardwareInfo()["serial"]` returns `637862dc2e4c6dd7`, matching
is case-insensitive and tolerates a leading `0x`, and a serial matching nothing is
refused rather than opening whatever happens to be attached.

Every analysed event stores `freq_raw_hz`, the measured centre before it is snapped to
the 6.25 kHz grid, so ppm is a query rather than a separate measurement:

```sql
SELECT ROUND(AVG(freq_raw_hz - freq_hz), 1) AS mean_error_hz,
       ROUND(AVG(freq_raw_hz - freq_hz) / (freq_hz / 1e6), 3) AS ppm
FROM events WHERE freq_hz = <the known transmitter> AND freq_raw_hz IS NOT NULL;
```

That query is still untested against hardware — it needs the **capture loop**, and
everything above came from `--spectrum`, which is a separate path. Running the capture
loop against a real radio is the first thing Phase 2 does.
