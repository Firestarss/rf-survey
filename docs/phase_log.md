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
| **4** — leave it running | — | — | 24 h, one radio; where detection thresholds get tuned |
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

**Outstanding for Gate 1**

- [x] Notch filter measured (step 9) — done 2026-08-26, PASS
- [ ] Both pads measured and labelled A/B, difference recorded (MiniSA, step 10)
- [~] Antenna-versus-dummy measured (step 11) — 2026-08-26. Needs a 5 dB pad, on order
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

**Six tones decoded correctly, capture ratio 0.71 to 1.0**, on live off-air
signals from 21 to 66 dB SNR. Together with the 74.4 and 110.9 decoded earlier,
that is eight distinct CTCSS tones identified correctly against a real
transmitter. This is most of what Phase 3's first table asks for.

**Path loss fits 28.9 dB per decade** — normal urban clutter, against 20 dB per
decade for free space and 30-40 for dense urban. Extrapolated to `on_db` of
10.0, the usable range of this chain is about **1.1 km**.

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

### Outstanding for Gate 2

- [ ] Zero overflows for an hour at 10 MSPS — needs the threading or optimisation work
- [ ] Phantom harmonics distinguished from real events
- [ ] An hour-long clean run, which neither of the above allows yet
