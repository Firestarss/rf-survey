# Phase log

Running record of gate results. One entry per phase, appended as it closes.
Full procedures live in `docs/bench-bringup.md`.

| Phase | Status | Date | Headline |
|---|---|---|---|
| **0** — Pi alone, no radio | **PASS** | 2026-08-18 | 28.8% of one core, 27 concurrent, peak 71.6 °C, zero throttling, fan confirmed |
| **1** — first radio, first signal | in progress | 2026-08-26 | radio 1 up; 7/7 FRS channels; ppm closed at −0.64 on three references; antenna steps outstanding |
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
ppm        -0.64   (three references; profile carries ppm: 0.64 to correct it)
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

**Step 12 done, 2026-08-26.** Three independent references, and they do not all agree
— which is the entire reason for having three:

| Reference | Reads | What it is |
|---|---|---|
| FRS handheld, 7 channels | -0.644 ppm | consumer TCXO |
| 464.000000 MHz transmitter | -0.65 ppm | licensed Part 90, tight tolerance |
| tinySA Ultra generator | -0.41 ppm | inexpensive lab generator |

The two agreeing to 0.01 ppm are a commercial transmitter and a handheld, measured on
different days at different frequencies on different signal types. The outlier is the
generator, exactly as `phase1-detail.md` warned. **The deck is low by -0.64 ppm and the
tinySA Ultra's output is +0.21 ppm high of what it displays** — worth writing on the
tinySA's case, since it is now a known property of the test gear.

The generator was confirmed to be the source by toggling its output off and re-capturing:
the 466.000 carrier vanished, and so did a spur at 462.1687 MHz sitting 15.6 dB below it.
466 MHz is a busy business allocation in Boston, so without that check the measurement
could have been made against someone else's transmitter entirely.

**`ppm: 0.64` is in the profile.** The sign is positive-means-signals-read-low, verified
on hardware rather than reasoned about: +0.64 brings a known carrier to within 96 Hz,
-0.64 doubles the error to -576 Hz.

**Zero clipping frames** at every gain from 0 to 45 with the handheld keyed nearby.
Overflows were zero on every capture except one taken while the test suite was running
on the same Pi, which produced 344 — the deck must not share the machine with CPU-heavy
work during a survey.

**Three software faults were found by the hardware**, none of which could have been
found without it: the device could not be opened at all, `--spectrum-seconds` ran for
half its stated duration, and captures took 3.1 GB. All fixed; see handoff section 8.

**Notch filter — Flamingo FM band-stop, measured 2026-08-26. PASS.**

A tinySA cannot sweep its generator and measure the result at the same time; that is a
tracking generator, and the two functions share hardware. The wiki's method needs two
tinySAs. So the tinySA generated and **the deck itself measured** — which is worth
noting as a capability, because it means the survey receiver can characterise its own
front-end parts without any other instrument.

Reference first, then the filter inserted, then subtract. Levels are absolute
(`floor + SNR`), not a difference of SNRs: the floor estimate moved up to 1.9 dB between
captures and subtracting SNRs directly would have credited that to the filter.

| Frequency | Reference | With filter | Result | Want |
|---|---|---|---|---|
| 88.0 MHz | -76.0 dB | -119.6 dB | **43.6 dB rejection** | >= 30 |
| 98.0 MHz | -76.0 dB | < -123.4 dB | **> 47.4 dB rejection** | >= 30 |
| 108.0 MHz | -76.5 dB | -111.3 dB | **34.8 dB rejection** | >= 30 |
| 466.0 MHz | -78.6 dB | -78.7 dB | **0.1 dB insertion loss** | <= 1.5 |

98.0 MHz fell below the detection threshold entirely, so its figure is a bound rather
than a value — measuring deeper needs more dynamic range than this setup has, since the
gain must stay low to keep Boston broadcast out of the measurement.

Test frequencies are whole megahertz deliberately: US FM stations sit on **odd tenths**
(88.1, 88.3 ... 107.9), so 88.0 / 98.0 / 108.0 fall between channels and no Boston
station sits on the generator. In this metro that is not a precaution to skip — at
gain 42 the bare cable picked up WBUR and WUMB at +58 to +70 dB.

**Step 11 — antenna versus dummy, 2026-08-26. Measured, and the procedure is wrong.**

Chain: `[antenna | dummy] -> Flamingo -> pads -> Airspy`, Nagoya NA-701 held vertical,
Boston metro. The dummy figure is unchanged by attenuation, as it must be — a pad in
front of a 50 ohm termination replaces one room-temperature resistor's noise with
another's, so what is being measured is the receiver's own noise either way.

| Pad | gain 39 antenna | gain 39 dummy | delta | gain 42 delta |
|---|---|---|---|---|
| 20 dB | -121.2 | -121.9 | **+0.7** | **+0.7** |
| 10 dB | -117.1 | -121.7 | **+4.6** | **+4.8** |
| 0 dB | -109.8 | -121.7 | **+11.9** | **+12.1** |

**The delta does not change with gain, and the procedure says to set it with gain.**
At 20 dB it reads +0.7 at gains 39, 42 and 45 — across a span that moves the noise floor
by 20 dB. That is not measurement scatter, it is the physics: above the ADC knee the gain
stages amplify the receiver's own noise and the antenna's equally, so the ratio is fixed.
Below the knee the converter swamps both and the delta collapses to zero. There is no
gain at which the ratio changes.

So `bench-bringup.md` and `phase1-detail.md` are both wrong on this step. Gain only has
to clear the knee — anywhere from 39 up. **What sets the delta is attenuation**, and
20 dB of it was throwing away the sky.

Fitting the three configurations gives external noise at **17.3x** the receiver's own
noise power before attenuation (independent estimates 17.5, 19.5, 14.8 — spread +/-1.4 dB
across a 12 dB range, so the model is sound):

| pad | predicted delta |
|---|---|
| 3 dB | 9.9 dB |
| 4 dB | 9.0 dB |
| **5 dB** | **8.1 dB  <- fit this one** |
| 10 dB | 4.4 dB |
| 20 dB | 0.7 dB |

**A 5 dB attenuator, not 20.** All of 3/4/5 satisfy the rule; the tiebreaker is the thing
the rule does not capture — overload headroom when a handheld keys ten feet from the
antenna. 5 dB keeps the most of it. It is also a single part rather than a stack, which
halves the connector count and gives a cleaner A-versus-B match between the two receivers
in step 10.

Zero clipping was measured at every pad value and every gain up to 45, including bare
antenna in Boston metro — **but with nothing keying nearby**, which is the case that
actually decides this. Untested.

This number is specific to the NA-701 at this bench. A festival site will differ, possibly
a lot; the measurement takes fifteen minutes to repeat on site. The VHF receiver needs its
own, and cannot inherit this one: different band, different antenna, and it rotates across
446 / 146 / 155 MHz, which are three different noise environments.

**Per-band attenuation model, measured 2026-08-26.** Nagoya NA-701, bare (no pad),
Flamingo in line, Boston metro, antenna propped vertical indoors.

Every delta below comes from a gain **verified linear by a compression sweep** — the floor
must rise ~10 dB per 3 gain steps, and where it does not, the reading is discarded.

| Band | delta | external/receiver | pad for 8-10 dB |
|---|---|---|---|
| 146 MHz (2 m) | 26.7 dB | 467x | **17-19 dB** |
| 155 MHz (MURS/VHF business) | — | — | **not measurable, see below** |
| 446 MHz (70 cm ham) | 12.9 dB | 18x | **3-5 dB** |
| 466 MHz (UHF business) | 13.5 dB | 21x | **4-6 dB** |

**Gain compression is real at VHF and it is silent.** At 146 MHz bare, the floor rises
+10.4 dB (33->36) and +10.2 (36->39), then only +6.2 (39->42) and +2.5 (42->45). Gain 39
is the last honest point: above the ADC knee, below compression. **Clipping frames read
zero throughout** — compression happens well before hard clipping, so the deck's existing
overload detection does not catch it. Every VHF reading taken at gain 42 earlier in the
session was compressed, which is what made the delta look gain-dependent.

**155 MHz cannot be characterised while paging is active.** The band holds a transmitter
at **152.600 MHz reading +55.4 dB**, 14 dB above anything else, plus more paging at 151.93
and 152.40 and a marine VHF cluster at 156.1-156.26 (Boston Harbor). Paging runs hundreds
of watts to kilowatts in bursts, so the front end is driven into compression intermittently
and the noise floor is not stationary between captures — measured floors of -108.6, -103.7,
-91.0 and -103.8 at successive gains, including a **12.8 dB fall for a 3 dB gain increase**,
which is not physically possible in a static environment. Fit 20 dB to linearise it, then
re-measure.

**The two receivers need very different attenuation, and `vhf` as configured cannot be
satisfied by any single value.** It rotates across 146, 155 and 446, which want roughly
18, 20 and 4 dB. Fitting 20 dB over-attenuates 446 by 16 dB, dropping its delta to ~1 dB
and losing weak 70 cm signals; fitting 5 dB compresses 146 and 155, which is a hard failure
that produces silently wrong data with no warning. **Take the 20 dB** — a sensitivity loss
is recoverable, invalid data is not. The better fix is architectural: 446 wants the same
4-5 dB as 466 and arguably belongs on the UHF receiver rather than grouped with the two
VHF windows. Not changed here; that is an architecture decision.

The original parts list assumed 20 dB for both radios. Right for VHF, four times too much
for UHF.

**Caveat on the 146 figure:** the antenna was propped on a computer desk, inside its near
field, and PCs are noisy at VHF. Some of that 467x may be the desktop rather than Boston.
Worth one check with the antenna elsewhere before buying on it — though the deployed deck
also sits beside a computer, so it is not unrepresentative.

**Outstanding for Gate 1**

- [x] Notch filter measured (step 9) — done 2026-08-26, PASS
- [ ] Both 20 dB pads measured and labelled A/B, difference recorded (MiniSA, step 10)
- [~] Antenna-versus-dummy measured (step 11) — 2026-08-26. Needs a 5 dB pad, on order
- [x] ppm confirmed against the MiniSA generator (step 12) — done 2026-08-26
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
