# Phase log

Running record of gate results. One entry per phase, appended as it closes.
Full procedures live in `docs/bench-bringup.md`, which defines what the phases are;
this file is the only place their status is tracked. The names below are copied from
that document's section headings and must stay identical to them — on 2026-08-27 the
two had drifted into describing different plans.

| Phase | Status | Date | Headline |
|---|---|---|---|
| **0** — Pi alone, no radio | **PASS** | 2026-08-18 | 28.8% of one core, 27 concurrent, peak 71.6 °C, zero throttling, fan confirmed |
| **1** — first radio, first signal | in progress | 2026-08-26 | radio 1 up; 7/7 FRS channels; ppm closed at −0.64 on three references; antenna steps outstanding |
| **2** — detection and logging | in progress | 2026-08-27 | detection and logging work; blocked on single-core saturation at 10 MSPS and phantom harmonics |
| **3** — tones | — | — | |
| **4** — leave it running | **running** | 2026-08-27 | started 21:24 UTC under systemd, unattended, parked on 466 |
| **5** — digital | — | — | DMR must not be mistaken for analog |
| **6** — second radio | — | — | dual-bus USB; needs a 2nd notch and antenna |
| **7** — repeater matching | — | — | |
| **8** — 24 hours, everything | — | — | |

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

**What Phase 1 changed in the software.** The measurements drove six code changes and
three corrections to these procedures; `docs/handoff.md` sections 8 and 9 carry the
detail. The two with consequences beyond Phase 1:

- a **front-end linearity check on every window**, because compression is invisible to the
  existing overload detection — clipping frames read zero right through it — and migration
  9 records the verdict per window so a compressed band cannot be mistaken for a quiet one
- the **receivers regrouped by required attenuation** rather than by service, 446 moving
  from `vhf` to `uhf`, because no single pad serves a receiver spanning a 4 dB need and a
  20 dB one

**Step 11 closed 2026-08-27 — the 5 dB pad fitted and measured. PASS.**

The attenuator kit arrived and the chain is now
`antenna -> adaptor -> Flamingo -> 2 dB -> 3 dB -> cable -> Airspy`. Measured with
`tools/padcal.py`, which is the same procedure Phase 1 ran by hand:

| gain | antenna | dummy | delta | state |
|---|---|---|---|---|
| 30 | -130.8 | -131.4 | +0.6 | edge |
| 33 | -129.3 | -130.9 | +1.5 | edge |
| 36 | -122.8 | -128.7 | +5.9 | inconclusive |
| 39 | -113.8 | -122.1 | **+8.3** | linear |
| 42 | -104.2 | -112.3 | **+8.1** | linear |
| 45 | -94.5 | -102.5 | **+8.0** | linear |

**Predicted 8.1 dB, measured 8.0-8.3 dB.** The delta is flat across 6 dB of gain,
which is the Phase 1 finding reproduced: above the knee the ratio does not depend
on gain. External noise re-derives at **17.3x the receiver's own** — the identical
figure Phase 1 fitted from the 20/10/0 dB configurations on a different day with
different parts. Two independent measurements, one answer.

**Gain 39 is now the floor, and that kills an assumption.** With 20 dB fitted, gain
30 was a usable setting and it was where overflows went to zero. With 5 dB fitted,
30 and 33 read `edge` and 36 `inconclusive` — the pad no longer holds the antenna
noise above the converter's own. **So there is no route to Gate 2 by desensing.**
The throughput problem has to be solved rather than avoided, and the long run will
be *busier* than the 11933 events/hour already measured, because the chain is now
7.6 dB more sensitive than the 20 dB / gain 42 configuration those came from.

Physically this is 2 dB + 3 dB stacked. Phase 1 argued for a single part to halve
the connector count; the kit has no 5, so the stack stands and the measurement
above is of the stack as built.

**Clipping with a transmitter beside the antenna — measured 2026-08-27. PASS.**

The one case the phase log had flagged as untested, and the one the 5 dB pad put
back in question: 20 dB of pad made it academic, 5 dB does not. GMRS handheld,
channel 20, high power, 2-3 m from the antenna, three ~10 s bursts, four runs.

```
  clip frames 0    desense frames 0     at gain 39 and gain 42
```

Three bursts logged at **64.7 / 63.8 / 62.4 dB**, durations 13.5 / 17.6 / 11.9 s
— full length, none of the truncation the no-pad ride produced. **DCS 074
decoded on all three**, which is what PT 52 should be on the Rocky Talkie list,
so the mapping table in `docs/tools.md` gets an independent confirmation.

**Splatter is a level threshold, not a gain one.** The +/-25 kHz skirts appear at
gain 39 and gain 42 alike whenever the peak clears ~62 dB:

```
  462.7000  +15.6 dB  DCS 074      <- +25 kHz
  462.6500  +13.6 dB  DCS 074      <- -25 kHz
```

One run peaked at 60.6 dB and showed none of it. I attributed that to the
operator standing further away; he was not, and everything recorded about the
two runs is identical — same serial, same gain, same centre, same pad. **The
4 dB difference is unexplained.** What does not depend on it: gain 39 does not
buy immunity from splatter, so the fix has to be in analysis.

**Gain is now 39 in the profile**, replacing the provisional 12. It is the
lowest gain padcal still calls linear (30 and 33 read `edge`, 36 `inconclusive`),
and all of 39/42/45 give the same +8.1 dB delta, so the lowest of them keeps the
most headroom.

### The frame the deck asks for is not the frame it gets

Chasing a cosmetic-looking `152.6 fps (target 76)` in the stats line found a real
fault. `frame_size()` picks 131072 samples at 10 MSPS; SoapyAirspy's stream MTU
returns 65536 and `getStreamArgsInfo` offers nothing to deepen it. The samples
are handled correctly — `_frame(self.chunk[:ret])` slices to what arrived — but
`frame_seconds` was computed from the size *requested*, and the detector counts
its thresholds in frames:

| setting | configured | in force at 10 MSPS |
|---|---|---|
| `min_duration_s` | 0.12 s | **0.059 s** |
| `hang_s` | 0.30 s | **0.151 s** |

Both halved, and **only at 10 MSPS** — at 2.5 MSPS the frame is 32768, fits
inside the MTU, and the arithmetic is right. Phase 2's detection gate ran at
2.5 MSPS, which is exactly why it passed every check and never showed this.

Visible in data already collected: **898 of `gym.sqlite`'s 12799 events are
shorter than the 0.12 s minimum that was supposedly in force** — about 7% of
that event rate. It does not overturn the periodic-beacon finding above, but
every 10 MSPS run to date, including both propagation walks, was detecting
against thresholds that were not the configured ones.

Fixed by clamping the frame to the stream MTU at startup, so one definition of a
frame serves the reader, the detector and the stats line. Verified on hardware:
`152.5 fps (target 153)`, zero overflows.

A second counter bug alongside it: the closing summary printed `analysed: 0`
while eleven events had demonstrably been analysed, because `_stats()` resets
`self.analyses` every interval and the summary printed that per-interval counter
as a session total. Split into `analyses_total`.

**Radio 2, read off the device 2026-08-27:**

```
serial     637862dc2f2f31d7     (uhf is ...2e4c6dd7 — check the whole string)
usb        Bus 002 Port 1, xhci-hcd, 480M   — different root hub from uhf's bus 4
firmware   AirSpy NOS v1.0.0-rc10-0-g946184a — identical to radio 1
power      in0_lcrit_alarm = 0 with both attached, 45.8 C
```

That is most of Gate 6's power and topology question answered. Phase 6 remains
blocked on a second notch filter and a second antenna, neither of which exist.

**Outstanding for Gate 1**

- [x] Notch filter measured (step 9) — done 2026-08-26, PASS
- [ ] Both pads measured and labelled A/B, difference recorded (MiniSA, step 10)
- [x] Antenna-versus-dummy measured (step 11) — closed 2026-08-27, +8.1 dB, PASS
- [x] Zero clipping frames at working gain, transmitter nearby — 2026-08-27, PASS
- [x] ppm confirmed against the MiniSA generator (step 12) — done 2026-08-26
- [ ] Everything in the spectrum accounted for (step 13) — provisional survey done
      2026-08-26 and no intermodulation found, but the gate wants the final pad fitted

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

---

## Phase 2 — in progress, opened 2026-08-27

**The capture loop met a real signal for the first time.** Everything before this was
`--spectrum`, which shares no code with it.

Run parked on one window with `--freq`, chain
`antenna -> Flamingo -> 10 dB -> 10 dB -> Airspy`, Nagoya NA-701 indoors, Boston metro.

### What works

| Test | Result |
|---|---|
| Events on the right frequency | **PASS** — 462.7125 logged with -75 Hz error, 0.16 ppm residual |
| Five presses ~2 s apart | **PASS** — five events, gaps 2.5-3.0 s |
| Five fast presses | **PASS** — one event, not five; `hang_s` merges correctly |
| Short stab | **PASS** — 0.38 s caught |
| Long transmission | **PASS** — tracks to 67.9 s; the old 1.27 s truncation is gone |
| CTCSS on live RF | **PASS** — 74.4 Hz and 110.9 Hz both decoded at capture ratio 1.0 |
| Tone declined when dwell too short | **PASS** — 0.38 s and 0.64 s events report `unknown` rather than inventing one |
| Temperature | **PASS** — 56.2 C |

The tone decodes are the first this project has done against a real transmitter, and
110.9 Hz was chosen deliberately: it is one of the two tones the DCS decoder used to
misread as a codeword before the capture-ratio ordering fix in handoff section 5.

### Blocker 1 — the loop saturates one core at 10 MSPS

```
                       2.5 MSPS      10 MSPS
detect per frame        1.34 ms       2.65 ms
analyse per event         33 ms         78 ms
achieved / required fps  76.3 / 76   124 / 152.6
overflows                      0        45-64
```

`readStream` returns 65536 samples, so real time at 10 MSPS needs 152.6 reads/sec and the
loop manages 124 — about 19% short, and that shortfall is the overflow. Measured at
**95-98% of one core**, and the loop is single-threaded, so the other three do not help.
Gate 2's "CPU across four cores under 40%" reads 24.6% and passes while the binding
resource is saturated; the gate is measuring the wrong thing.

There is a feedback loop in it: overflows drop samples, so signals appear to stop, so
events fragment, so more events need the 78 ms analysis, which blocks the reader further.

**10 MSPS is not optional.** Repeater pairing needs 462.x and 467.x heard together, 5 MHz
apart, which a 2.5 MSPS span cannot do. Phase 2 was run at 2.5 MSPS because every check in
the gate is valid there, but the throughput problem has to be solved before deployment.
The obvious candidate is moving analysis off the read thread.

### Blocker 2 — strong signals manufacture phantom events that look real

A handheld at 10 feet reading 64.8 dB SNR produced **124 phantom events on 124 channels**,
spanning the entire 2.49 MHz window, all starting within a millisecond of the real one.
At gain 42 they were broadband desense and were correctly flagged `overload`.

Dropping gain 42 -> 30 removed 12 dB of **VGA**, which sits after the mixer:

| offset | gain 42 | gain 30 |
|---|---|---|
| carrier | 64.8 dB | 67.3 dB |
| +150 kHz | 57.0 (-7.8 dBc) | 18.3 (-49.1 dBc) |
| phantom channels | 127 | 11 |

**41 dB of spur improvement for 12 dB less gain** — far more than 1:1, so the products are
generated at or after the VGA, converter-side. The carrier read *stronger* at the lower
gain, because compression had been flattening it too.

The remaining products are odd harmonics of the carrier's **baseband offset from the tuned
centre**. Measured with the carrier 112.5 kHz above centre:

```
  observed      offset    N        freq err   err/N
  462.7125    +112.5k    +1.00       -75 Hz     -75
  462.2625    -337.5k    -3.00      +227 Hz     -76
  463.1625    +562.5k    +5.00      -378 Hz     -76
  461.8125    -787.5k    -7.00      +529 Hz     -76
  463.6125   +1012.5k    +9.00      -680 Hz     -76
  461.3625   -1237.5k   -11.00      +840 Hz     -76
```

Exact odd integers. It is why the comb spacing changed from 150 kHz to 450 kHz when the
transmitter moved from channel 1 to channel 7 — the baseband offset went from -37.5 to
+112.5 kHz.

**These are the dangerous ones.** Being harmonics of a real signal they inherit its
properties: they carried CTCSS 110.9 Hz at capture ratio 1.0, matched its duration to
14 ms, and were **not** flagged `overload`, because they are discrete products rather than
the broadband lift `OverloadMonitor` watches for. Every field the deck records makes them
look like genuine traffic on channels nobody keyed.

**They are detectable.** `freq_raw_hz - freq_hz` is exactly N times the parent's, because
the harmonic multiplies the offset error along with the offset. A real transmitter's
frequency error bears no relation to how far it happens to sit from the deck's tuned
centre. Not yet implemented; it needs a design decision about whether to drop such events,
flag them, or record the parent they derive from.

### Also found

- **stdout was block-buffered.** A 45 s run that logged 124 events to the database emitted
  none of them to its log file: Python block-buffers stdout when it is not a terminal, and
  `timeout` sends SIGTERM, so the buffer died with the process. journald is a pipe too, so
  the deployed deck had the same hole. Now line-buffered.
- **The linearity check cannot see this.** It runs once when a window opens, against
  whatever is on the air at that moment, so compression caused by an intermittent strong
  signal is invisible to it. It reported `linear` for the window in which all 124 phantoms
  appeared, and it was right at the time it looked.
- **The overload hint recommended 20 dB** as costing "no usable sensitivity", which Phase 1
  measured as wrong — 20 dB leaves the antenna-versus-dummy delta at 0.7 dB. Corrected.

### 2026-08-27, later: what the throughput work actually bought

**The FFT was doing double-precision work on single-precision data.** `np.fft`
promotes complex64 to complex128; scipy.fft respects the dtype. 2.454 -> 1.223 ms
per frame for that alone, and 0.762 with two workers.

**Analysis moved off the read thread.** One 91 ms `analyze_analog` against a
6.55 ms frame is fourteen frames arriving with nobody collecting them, and the
Airspy's USB buffer is 65536 samples with no way to deepen it —
`getStreamArgsInfo` returns nothing at all. That latency, not average CPU, is
what the overflows were. The job queue is two deep because each job carries
~96 MB of IQ at 10 MSPS; when it is full the analysis is skipped and counted,
which loses one row's detail rather than corrupting a whole window.

**Two measurements that reversed a decision.** scipy's `workers=2` is clearly
best in isolation and clearly worse in the running deck, because its threads
contend with the analysis thread for the GIL:

| | detect | fps | overflows |
|---|---|---|---|
| workers=1 | 2.23 ms | 143.5 | **22** |
| workers=2 | 2.61 ms | 137.6 | 38 |

And reading the Airspy's **native CS16 is worse than asking for CF32** — 2.279 ms
per frame against 1.820 — because the driver's conversion beats anything numpy
does. That closed off the obvious next optimisation.

**Where the budget actually goes**, per 65536-sample frame at 10 MSPS against a
6.55 ms real-time budget:

```
readStream           1.82 ms   28% of one core   IRREDUCIBLE
periodogram          0.79 ms   12%
everything else      0.56 ms    8%
                     -------
reader total         3.17 ms   48%
```

`readStream` costing 28% of a core to do nothing but read had never been
measured, and it is the single largest item. Detector.step is 0.095 ms — 1.4% —
so the detector was never the problem.

**Result: 63 overflows -> 22 at gain 42, and zero at gain 30.** Not yet a pass.
The remaining gap is GIL contention: the reader needs 3.17 ms of work per frame
and gets 7.04 ms of wall time when the analysis thread is busy. Beating that
needs analysis in a separate *process*, which means shared memory for 96 MB
jobs, or an event rate low enough that analysis is not continuous.

**The event rate is the real variable.** Boston at 466 MHz with `on_db = 10.0`
produced **11040 events/hour** at gain 42 and almost none at gain 30. Those
thresholds have never been tuned against real traffic — they were set from
synthetic signals — and Phase 4 is where that happens. It is likely that the
honest fix here is a threshold, not a thread.

> **Corrected 2026-08-27 — the sentence above is wrong.** Re-examining
> `data/gym.sqlite` (12799 events, 191 channels, one 4001 s window, zero overload
> flags, 57 harmonics) shows the rate is not threshold junk but real periodic
> traffic, and no threshold removes it:
>
> | `on_db` | events/hr |
> |---|---|
> | 10.0 | 3966 |
> | 18.0 | 3042 |
> | 25.0 | 2291 |
>
> Fifteen dB of sensitivity thrown away buys a 42% reduction. The reason is
> visible in the timestamps — the busiest channels are **periodic emitters with
> stable per-channel phase**, which noise does not do:
>
> ```
>   462.125   ...357.11  419.29  481.88  542.57    period 62.2, 62.6, 60.7 s
>   463.625   ...361.39  423.57  486.03  546.51    period 62.2, 62.5, 60.5 s
>   462.075   ...362.09  424.25  486.72  547.15    period 62.2, 62.5, 60.4 s
> ```
>
> About twenty channels across 461-470 MHz sending 4 s bursts once a minute at
> ~20 dB SNR, each on its own fixed offset, plus a second family (463.900,
> 462.050, 462.500, 470.2625, 464.250) doing 0.17 s bursts every 9.53 s with a
> gap CV of 0.07-0.20. What they *are* is unidentified — UHF telemetry of some
> kind — and captured audio is what would settle it.
>
> **So the analysis thread is continuously busy in Boston by right, and the
> 10 MSPS throughput problem cannot be tuned away.** Whether a festival site is
> quieter than a Boston rooftop is plausible and unproven.

### 2026-08-27, evening: the first propagation data this project has

A GMRS handheld at 5 W on 462.675, transmitted from seven surveyed points on a
walk home, each with a different CTCSS so the deck's own decode identifies which
transmission it was. Receiver at 42.3854086, -71.0796309, chain
`NA-701 indoors -> Flamingo -> 10 dB -> 10 dB -> Airspy`, gain 42, 10 MSPS.

| CTCSS | Code | Distance | SNR |
|---|---|---|---|
| 162.2 | 26 | 10 m | 66.5 dB |
| 156.7 | 25 | 56 m | 53.3 dB |
| 151.4 | 24 | 109 m | 37.4 dB |
| 146.2 | 23 | 122 m | 38.9 dB |
| 141.3 | 22 | 227 m | 29.5 dB |
| 136.5 | 21 | 412 m | 21.2 dB |
| 131.8 | 20 | **492 m** | **NOT HEARD** |
| 167.9 | 27 | **86 m** | **NOT HEARD** |
| 173.8 | 28 | 209 m | 22.6 dB |

**Six tones decoded correctly, capture ratio 0.71 to 1.0**, on live off-air
signals from 21 to 66 dB SNR. Together with the 74.4 and 110.9 decoded earlier,
that is eight distinct CTCSS tones identified correctly against a real
transmitter. This is most of what Phase 3's first table asks for.

**Corrected 2026-08-27 after checking the geometry — the first reading of this
was wrong, and so was the second.** Sorted by bearing:

| code | distance | bearing | residual vs fit |
|---|---|---|---|
| 24 | 109 m | 351 N | -0.7 dB |
| 23 | 122 m | 41 N | +2.3 dB |
| 22 | 227 m | 44 N | +1.2 dB |
| 21 | 412 m | 99 E | +0.7 dB |
| 28 | 209 m | 203 S | -6.9 dB |
| 27 | 86 m | 195 S | **not heard** |
| 20 | 492 m | 114 ESE | **not heard** |

The tempting reading — two dead bearings, coverage as a shape rather than a
radius — does not survive the geometry. **27 and 28 are 8 degrees apart, the
same line, and the FURTHER of the two is the one that got through**: nothing at
86 m, +22.6 dB at 209 m. A directional null does not do that. And 27 and 20 are
**82 degrees apart**, nearly a right angle, so they were never one pattern.

What actually fits: **at short range the receiver's own building dominates.**
The 86 m path crosses the whole house to reach a receiver sitting in an upstairs
room; the 209 m path, further down the same street, arrives at the window on a
different geometry and gets in. Distance is not the variable at that scale —
walls are.

**The gym miss at 492 m and 114 degrees remains unexplained.** It is on its own
bearing, well clear of the house effect, and 21 at 99 degrees and 412 m came in
at +0.7 dB against the fit. Either something specific blocks that path or that
transmission did not happen as intended. Not resolved, and not worth a story.

**A confound that runs through every number here: the antenna was indoors, in a
room, for all of it.** A deployed deck with the antenna outside would produce a
different map, and probably a much better one. These figures are a lower bound
on what the hardware can do, not a measurement of it.

**Path loss fits 30.5 dB per decade, R^2 = 0.937, residual sigma 3.8 dB** —
normal urban clutter, against 20 dB per decade for free space and 30-40 for
dense urban. The fit is *good*, which is the point: where the deck has a path,
distance predicts the level within a few dB. Extrapolated to `on_db` of 10.0 the
usable range is about **910 m** along a clear bearing.

**The gym transmission was obstruction, not range, and three earlier readings of
it here were wrong.** 492 m should have delivered ~20 dB by the fit, and 412 m
actually delivered 21.2 dB, so 80 m more should have cost 1.5 dB. It delivered
nothing — a hole of more than 20 dB on that one path. The deck hears a 5 W
handheld at 412 m perfectly well **with the 20 dB pad fitted**, so the pad was
never why the gym failed. A building was.

**The attenuation still matters, for a different reason.** At 28.9 dB/decade,
recovering 20 dB of noise figure is 4.9x the range: ~1.1 km now, ~5.5 km with
the 5 dB pad Phase 1 measured. Worth having, but it is not what silenced the gym.

**Siting beats sensitivity, and that is the finding to carry to a festival.** One
building cost more than quadrupling the deck's range would buy back. Where the
antenna stands at the event will matter more than any threshold tuned on this
bench. It also means a single deck cannot be assumed to cover a site: the honest
coverage claim is line-of-sight, not radius.

### A second phantom mechanism, still unhandled

The closest transmission, at 66.5 dB, put skirts on neighbouring channels:

```
  462.6500   snr 17.7   CTCSS 162.2  cap 0.94     <- 25 kHz below
  462.7063   snr 17.2   CTCSS 162.2  cap 0.94     <- 31 kHz above
```

They carry **the parent's tone at high confidence**, exactly like the odd
harmonics, and are just as convincing in the database. But they are 25-31 kHz
out, not odd multiples of the baseband offset, so the harmonic detector added
this morning correctly does **not** flag them — they are adjacent-channel
splatter from a very strong signal, a different mechanism.

The detector's local-maximum rule covers +/-1 channel, which is 6.25 kHz, chosen
because FRS primary and interstitial channels interleave at 12.5 kHz and a wider
rule would discard real traffic. These land four and five channels out, well
beyond it. Unsolved, and it only appears at SNRs above about 60 dB, which at a
festival means anyone keying within a few tens of metres of the deck.

### 2026-08-27, night: the attenuation trade, measured at both ends

A second walk, 18 points, **both 10 dB pads removed** so the only change from the
earlier walk was the attenuation. Path loss reproduced independently:
**30.5 dB per decade against 30.4** from the first walk.

**Removing 20 dB of pad bought 7.3 dB, not 20.** At the same spot, 412 m on
bearing 99: 21.2 dB with the pads, 28.5 dB without. The prediction offered
beforehand was 20 dB and it was wrong, because a pad does not add its face value
to the noise figure when external noise is already present. Referred to the
antenna, with external noise measured at 14.8x the receiver's own:

```
    pad 20 dB -> total noise = 114.8 x receiver noise
    pad  0 dB ->               15.8 x
    improvement = 10*log10(114.8/15.8) = 8.6 dB
```

Predicted 8.6, measured 7.3. **A pad can never return more than the margin by
which the receiver's own noise was dominating.** Usable range went 910 m to
1850 m, the 1.9x that 8.6 dB predicts at 30.5 dB/decade.

**And the other end of the trade, from 9 metres with no pad:**

| | |
|---|---|
| events logged in 26 s | **2925** |
| distinct channels lit | **1360** |
| flagged `overload` | 2590 |
| identified as harmonics | 1042 |
| real signal SNR | **55.4 dB — down from 66.5 with 20 dB of pad** |
| real signal duration | **6.67 s and 4.02 s, from two ~20 s holds** |

More input producing less SNR is compression, unambiguously. **Overload does not
corrupt everything equally**: the tone decoded correctly (DCS 072 and 073, both
right, on a signal this far into compression) and the frequency was right. What
it destroyed was **level and duration** — which are precisely what an airtime
survey exists to measure.

Note also that the real transmission was flagged `overload=0`. The 2590 flags
landed on the phantoms; the one event most corrupted by the overload is the one
that does not say so.

**So both ends of the attenuation trade now have measured failures:**

| pad | failure |
|---|---|
| 20 dB | misses a 5 W handheld at 492 m |
| 0 dB | 2925 phantom events from one keyup at 9 m, real signal compressed |

That is the case for the 5 dB Phase 1 measured, and it is now bounded on both
sides by data rather than by one measurement and an argument.

**A methodological note worth keeping.** The DCS half of this walk was labelled
from an inferred code mapping, and a later "correction" shifted every DCS point
by one position. Both mappings produced plausible fits — 30.5 and 22.3 dB per
decade. Choosing between them by fit quality would have been circular. It was
settled by the manufacturer's spec sheet, with a mapping-independent
cross-check in the meantime: the CTCSS-only points gave 34.9 dB per decade and
the earlier all-CTCSS walk 30.4, which bracket the correct mapping and exclude
the wrong one. The confirmed table is in `docs/tools.md`.

### Outstanding for Gate 2

- [ ] Zero overflows for an hour at 10 MSPS — needs the threading or optimisation work
- [ ] Phantom harmonics distinguished from real events
- [ ] An hour-long clean run, which neither of the above allows yet

---

## Phase 4 — running, opened 2026-08-27 21:24 UTC

**The first genuinely unattended run.** Started under systemd with the operator
leaving for several days, which is the condition Phase 4 has been waiting for —
the gate asks for 24 hours of nobody touching it.

```
unit      rfsurvey@uhf, enabled, Restart=on-failure, StartLimitBurst 30/30min
chain     Signal Stick -> adaptor -> Flamingo -> 2 dB -> 3 dB -> cable -> Airspy
radio     637862dc2e4c6dd7, USB bus 4
tuning    462.8 MHz parked, 2.5 MSPS, gain 42, ppm 0.64
detect    on 10.0 / off 6.0 dB, min 0.12 s, hang 0.30 s  (correct for the
          first time on a 10 MSPS run — see the frame/MTU fix above)
database  data/phase4.sqlite, run 2
audio     data/captures/run2, 40 GB budget
opening   front end linear, overflow 0, clip 0, desense 0
```

**Parked rather than rotating.** The band-switching trap in `bench-bringup.md` is
documented and unverified: a retune that carries noise-floor state across bands
produces a burst of false detections after every switch. That is a variable to
add while somebody is watching, not to a run nobody is. 466 is also the band the
survey exists for.

**10 MSPS deliberately, with Gate 2 unpassed at that rate.** No bench measurement
can say what the overflow rate is under real diurnal load over days, and that is
the number the deployment decision needs.

### What this run is expected to answer

- overflow rate at 10 MSPS across a full daily traffic cycle, not a 90 s bench window
- memory flat over days, or not
- disk growth against the projection of ~206 bytes/event and ~59 MB/day
- **what the periodic emitters on 461-470 actually are** — the ~62 s / 4 s family
  and the 9.53 s / 0.17 s family. Audio capture is on specifically so these can be
  identified offline rather than guessed at
- whether `on_db = 10.0` is right against real traffic, which is the gate's own
  question and has never been answered against anything but synthetic signals

### False start: three hours of measurements taken into a dummy load

`tools/padcal.py` ends its sweep with the operator having fitted a 50 ohm
terminator, and nothing told him to put the antenna back. Everything measured
after it went into that terminator: the "nothing unexplained in the spectrum"
survey, all four Gate 1 clipping runs, and the first start of this Phase 4 run.

**Caught by the event rate**, not by any check: zero events in ninety seconds on
a chain more sensitive than the one that had produced 12000 events/hour. The
confirmation was already in the logs — the spectrum survey reported a band
reference level of -104.7... -112.2 dB, and padcal had just measured that same
gain at -104.2 antenna and -112.3 dummy.

**What it invalidated:** the Gate 1 clipping result (`clip 0, desense 0` proves
nothing into a terminator — that row is re-opened), the spectrum survey, and the
opening `front end linear` verdict.

**What survived:** padcal itself, which swaps both loads explicitly; the
frame/MTU arithmetic; and the DCS 074 decode, which is an identification rather
than a level.

**And it explained something previously written down as unexplained.** Two
gain-39 runs peaked at 60.6 and 64.7 dB with everything recorded about them
identical. That was attributed here to the operator having moved; he had not,
and said so. Coupling into a terminator is leakage, which depends sharply on
exactly how a handheld is held — 4 dB between takes is expected there and would
be odd through an antenna. The operator's pushback was right and the explanation
offered him was wrong.

Two guards added, because "be more careful" is not a mechanism:

- padcal now prints `*** THE DUMMY LOAD IS STILL FITTED — REFIT THE ANTENNA ***`
  before its results, where it cannot be missed in the scroll.
- The deck checks its own floor against `dummy_floor_dbfs` in the profile once
  per window and warns when the antenna appears to be contributing nothing. A
  warning, never a refusal — a genuinely quiet site is possible, and a deck that
  will not run in a field because it disagrees with a config value is worse than
  one that logs a loud line and continues.

### The antenna changed, and so did the working gain

The NA-701 left with the operator; a Signal Stick replaced it. Re-measured
immediately, which turned out to matter:

| | NA-701 | Signal Stick |
|---|---|---|
| delta at 5 dB pad | +8.1 dB | **+7.3 dB** |
| external / receiver noise | 17.3x | **13.1x** |
| lowest linear gain | 39 | **42** |
| pad the rule wants | 5 dB | 2-4 dB |

**The working gain is a property of the antenna, not the radio.** Gain 39,
measured and committed to the profile hours earlier, reads `inconclusive` on
this antenna. Re-run padcal after any antenna change.

The 5 dB pad is kept though the rule prefers 2-4: the difference is 7.3 dB of
delta against 8.8, under a dB of system noise, and overload is the failure this
project has measured as the more damaging one.

One padcal bug noted and not fixed: it printed `FIT 4 dB (anything from 2 to
4 dB satisfies the rule)` and then `Currently fitted: 5 dB (already right)`.

**A failure mode worth recognising.** The first Signal Stick sweep was run with
the antenna never removed, so both halves measured the same load. It returned
delta +0.2 dB and "external noise 0.5x the receiver's own" — which reads as a
plausible very-quiet-site result rather than an error. Measuring one load twice
looks like a real measurement.

### Gate 2, measured under real traffic at last

The run started at 10 MSPS on the corrected chain and could not keep up:

```
  10 MSPS   120.5 fps against target 153   overflow 207   16169 events/hr
   2.5 MSPS  76.3 fps against target 76    overflow   0    4320 events/hr
```

26% short, sustained, with overflows climbing continuously. **This is the honest
answer to the question this run was partly meant to ask, and it took five
minutes rather than three weeks.**

So the unattended run drops to 2.5 MSPS. Overflows are dropped samples and
dropped samples corrupt level and duration, which are the two things an airtime
survey exists to measure; three weeks of 26%-lossy data over 10 MHz is worth
less than three weeks of clean data over 2.5 MHz. Centred 462.800 rather than
466.000 — the span 461.55-464.05 holds every FRS/GMRS output channel plus the
whole periodic-emitter cluster this run is meant to identify, and gives up the
467.x repeater inputs that Phase 7 needs and this gate does not.

Gate 2 still needs analysis in a separate process before 10 MSPS is deployable,
and the festival needs 10 MSPS for repeater pairing.

### Known limitations of this run, recorded before it produces anything

- **One radio.** Phase 6 needs a second notch filter and a second antenna,
  neither of which exist. Radio 2 is attached, enumerated and idle on bus 2.
- **Antenna is indoors**, as it was for both propagation walks. Every level here
  is a lower bound on what the hardware can do outdoors.
- **Adjacent-channel splatter is unhandled.** Anything keying within a few metres
  and clearing ~62 dB SNR will put DCS-carrying skirts +/-25 kHz out, at any gain.
- **Step 10 of Gate 1 was skipped** — pads A/B on the MiniSA. Deliberate: padcal
  cross-checked the fitted 5 dB against Phase 1's independent 17.3x fit, and the
  hour was better spent elsewhere.
- **The Gate 1 clipping test is re-opened and could not be redone** — it needs a
  transmitter keyed beside the antenna and the operator had to leave. The only
  version of it that exists was measured into a terminator and is worthless. It
  is the first thing to do on return, and until it is done there is no evidence
  that the 5 dB pad survives somebody keying next to the deck.
- **Gain 42 is the lowest linear setting on this antenna**, so there is no
  headroom below it. On the NA-701 there was 3 dB.

### First thing on return

1. Redo the Gate 1 clipping test — handheld at 2-3 m, high power, antenna fitted.
2. Identify the periodic emitters from the captured audio.
3. Gate 2: analysis in a separate process, which is what 10 MSPS needs.

---

## 2026-09-16 — the rust engine, and WildFire readiness

Two receivers at 10 MSPS did not fit on the Python engine: 2,177 and 1,899
overflows in 6.5 minutes, one radio on a dummy load hearing nothing. The
per-frame path allocated several half-megabyte arrays every 6.6 ms frame, and
copied up to ~96 MB of IQ per analysed event on the reader thread.

`engine/` is a Rust reader, spectrum, noise floor and detector for one receiver.
Python keeps the database, rotation, captures and messages (`src/engine_loop.py`),
and analysis runs in its own process reading IQ from shared memory. Trusted on:

- `tests/test_engine_equivalence.py`: identical starts and ends, by channel and
  sample position, against the real Python classes, at both rates, including a
  centre on the grid where numpy's round-half-to-even matters
- `tests/test_engine_loop.py`: identical database rows through the whole deck,
  including tone, DCS, deviation and measured frequency to the hertz
- hardware, both radios at 10 MSPS through a full rotation: **0 overflows each**
  under ~22,000 events/hour, 1.8 ms per frame, load 2-3

Found on the way and fixed: two receivers starting together corrupted the schema
version (a race in `init_schema`, reproduced by a test, fixed with a file lock);
WildFire's audio would have overwritten Phase 4's by run number; `run()` crashed
on hand-built argument namespaces; and a file-driven run claimed a real radio's
serial.

One analysis worker, not two: a second cost the engines headroom and a dropped
block, and lowering its priority did not buy that back.

Then an overnight soak under the real units, and `tools/wildfire-ready` to report
it and prepare the deck. Field instructions: `docs/wildfire.md`.
