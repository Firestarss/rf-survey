# Design decisions

What we chose, what we chose against, and why. This replaces the earlier design doc
and build guide, both of which described a three-receiver architecture we've since
narrowed.

Read `rf-primer.md` first if any radio terms are unfamiliar.

---

## What this is

A box that watches several radio bands at once, notices every time someone
transmits, works out enough about each channel that you could program it into a
radio and talk on it, and writes it all to a searchable database. Runs unattended
for a day at a time.

**The completeness bar:** could you fill in a full radio channel entry from this
row? Frequency alone is trivia. Frequency plus tone plus — for a repeater — the
input frequency and *its* tone, is useful.

| Tier | You know | Can you talk? |
|---|---|---|
| T0 | a frequency was busy | no |
| T1 | + what kind of signal | no |
| T2 | + the tone | yes, simplex |
| T3 | + input frequency and its tone | yes, through the repeater |
| T4 | + digital details | yes, digital |

**T2 is the minimum. Anything below it is trivia.**

---

## The two ideas everything else follows from

### 1. Watch, don't scan

A scanner steps through channels one at a time. At a festival, transmissions last
one to three seconds, so a scanner stepping 1000 channels misses nearly everything.

Instead, one FFT of a wide capture gives power at every frequency at once — all
1600 channels for roughly the cost of one operation. Nothing inside the window is
ever missed. Closest analogy: reading a whole GPIO port instead of polling 32 pins.

### 2. Everything at a festival is loud, so throw signal away

A 2 W handheld arrives at −2 dBm from 3 m away and −62 dBm from 3 km. Background
noise is around −133 dBm. So even the most distant signal you care about is 65 dB
above the noise floor.

You have **surplus sensitivity and a severe shortage of headroom.** Someone keying
3 m from the box will clip the ADC, and a clipping RF front end doesn't just
distort — it manufactures signals at frequencies where nothing is transmitting. For
a box whose job is finding channels, that's the worst failure mode: it doesn't
crash, it just quietly reports fiction.

| Attenuation | SNR on the most distant signal | 5 W radio at 3 m |
|---|---|---|
| none | 65 dB | clips — **broken** |
| 10 dB | 55 dB | clips — **broken** |
| **20 dB** | **45 dB — plenty** | **survives** |

**So: a permanent 20 dB attenuator on every receiver.** This is also the answer to
"I can't survey the site in advance" — you don't tune for a site, you build so the
site doesn't matter.

---

## D1 — SDR family: **Airspy**

**Requirement:** ~64 dB of clean range and ~9 MHz of bandwidth per window.

| Option | Bandwidth | Clean range | Cost | Why not |
|---|---|---|---|---|
| RTL-SDR V4 | 2.4 MHz | ~50 dB | $40 | Short on both. Four dongles per window, and still invents signals. |
| **Airspy R2** | **10 MHz** | **~70 dB** | **$169** | **chosen** |
| Airspy Mini | 6 MHz | ~70 dB | $99 | Not enough bandwidth for the UHF window (see D4) |
| SDRplay RSPdx | 10 MHz | ~75 dB + filters | $250 | see below |
| HackRF One | 20 MHz | ~50 dB | $320 | 8-bit in a dense site is exactly the failure we're avoiding |
| LimeSDR Mini 2 | ~30 MHz | ~70 dB | $400 | More bandwidth than a Pi can process |

**The close call was the SDRplay RSPdx**, which has switchable filters built in —
attractive when your whole strategy is "filter defensively." But its filter bank
tops out at a single 420–1000 MHz band, so above 420 MHz there's no selective
preselection at all, and both of our UHF windows sit inside that one broad filter.
It helps most at VHF and HF, and does almost nothing where our traffic is. Its
headline improvement over cheaper models is HF performance, which we cut. And the
driver is a closed binary blob.

**Known cost of choosing Airspy:** `libairspy` converts samples on the host, not in
the radio. An R2 sends 20 million real samples/sec and the driver turns them into
10 million I/Q pairs on your CPU before any of your code runs. Roughly 2 GFLOP/s per
radio. It's SIMD-optimised and manageable, but it's real and it scales with receiver
count.

## D2 — Receiver count: **two**

Not a preference — a hard limit. See D7.

## D3 — Windows: **profiles, not fixed**

Different environments want different bands. The antenna, filter and attenuator all
screw on and off, so they're profile kit rather than design commitments. The deck
just needs two bulkhead connectors.

**First profile (WildFire, burns):**

| Receiver | Tuned to | Duty | What's in it |
|---|---|---|---|
| 1 | **466.0 MHz** | parked | Every FRS and GMRS channel, the frequencies GMRS repeaters listen on, and Part 90 business — both halves of all of it |
| 2 | **446.0 / 146.0 / 153.2** | 33% each | 70 cm ham; 2 m ham; MURS and low VHF business |

**Why 466.0 specifically.** Usable span works out to about 461.5–470.5 MHz, which
puts every frequency that matters comfortably inside:

| Frequency | What | Position in the window |
|---|---|---|
| 462.5500 | lowest GMRS output | 77% |
| 464.5000 | itinerant business output | 33% |
| 467.7250 | GMRS repeater input | 38% |
| 469.5500 | highest itinerant input | 79% |

Part 90 business repeaters use the same +5 MHz spacing as GMRS, so this one window
brackets both halves of essentially every UHF land-mobile pair in the US, and the
repeater-matching code works on them unchanged.

**Deliberate sacrifice:** tuning to 466.0 gives up 460.0–461.5, where some police and
fire repeaters transmit. Tuning to 465.0 would keep those but push 469.550 past the
usable edge. Event operations traffic matters more here than police dispatch.

**Why duty cycle barely matters.** Chance of catching a channel at least once in 24 h:

| How often used | Parked | 50% | 33% |
|---|---|---|---|
| Every 12 min | 100% | 100% | 100% |
| Once an hour | 100% | 100% | 100% |
| Every 4 hours | 100% | 95% | 86% |
| 2–3 times a day | 91% | 70% | 55% |

Nothing separates until a channel is used a couple of times a *day*. So park the
busiest window and share the quiet ones.

## D4 — Models: **two Airspy R2**, $338

Both windows need more than 6 MHz, which rules out the cheaper Mini. A Mini on the
466 window would put the GMRS extremes at 94–97% of its usable width and would miss
469.500/469.550 entirely — the inputs for the 464.500/464.550 business repeaters.
You'd hear those repeaters and never learn how to key them.

## D5 — Compute: **Raspberry Pi 5, 8 GB**

| Board | CPU | Price | Verdict |
|---|---|---|---|
| Pi 4 | 4× A72 @ 1.8 | $75 | Both USB3 ports share one link, no NVMe. $5 saved for half the headroom. |
| **Pi 5 8 GB** | 4× A76 @ 2.4, NEON | **$80** | **chosen** |
| Radxa X4 | 4× N100 @ 3.4, **AVX2** | $80 | ~2× the DSP throughput at the same price — but runs hot, vendor cooler reported inadequate, docs thin |
| Rock 5B+ | RK3588, 8 cores | $180 | ~1.5× a Pi 5, twice the price |
| Jetson Orin Nano | 6× A78 @ 1.7 | $249 | GPU can't help branchy decoders; CPU is Pi-5 class |

Chosen for maturity, a known-good thermal solution, and a community that has already
posted about whatever breaks at 3 a.m. **The Radxa X4 is a clean $80 escape hatch** —
same size, same price, roughly double the CPU — if Phase 2 shows the Pi is short.

## D6 — Storage: **NVMe on the official M.2 HAT+**

2230 or 2242 drives only; a normal 2280 laptop drive will not fit. USB storage was
considered (see D7) and rejected: for a box writing unattended for 24 hours, USB
enclosures drop off the bus and need quirk workarounds. NVMe is the reliable option
and uses the PCIe connector, which doesn't compete with USB at all.

## D7 — USB: **one radio per port, no hub**

**This is what limited us to two receivers.**

A USB 3 hub does *not* give USB 2 devices more bandwidth — a USB3 hub is a USB2 hub
and a USB3 hub sharing one cable, and USB 2 devices connect back at 480 Mbit
regardless. I recommended a multi-TT hub as the fix for this and was wrong; it fixes
scheduling, not capacity.

The Pi 5's RP1 chip has two xHCI controllers, each with a single USB 2.0 PHY. So:
**two independent USB 2.0 buses, roughly 40–45 MB/s each.**

| Device | Sends | Share of one bus |
|---|---|---|
| Airspy R2 @ 10 MSPS | ~30 MB/s | 70% |
| Airspy Mini @ 6 MSPS | ~18 MB/s | 45% |

Two buses, and three radios would need 78 MB/s split as 48/30 — and 48 doesn't fit in
45. No hub arrangement fixes it. Turning one down doesn't either: an R2 only drops to
2.5 MSPS (2.2 MHz, too narrow) and a Mini to 3 MSPS (2.7 MHz, just short of MURS).

**Two radios also removes the powered hub entirely.** Two Airspys draw about 1 A
against the Pi 5's 1.6 A budget, so they run straight off the board. Three would have
been 1.5 A and uncomfortable.

**Considered and deferred:** a PCIe USB card in place of the NVMe HAT, with storage
moving to a USB 3 SSD. The reasoning is sound — SuperSpeed uses separate wires from
USB 2.0, so an SSD costs zero USB 2 bandwidth. But how many extra buses a card
actually adds is card-specific and unknown without testing, PCIe compatibility on the
Pi 5 is finicky, the card needs slot power the FPC connector doesn't supply, and it
trades the most reliable storage for the least. Revisit after WildFire, when there's
evidence about whether the third radio is needed.

## D8 — Power: **bench PSU for now**

Official 27 W USB-C supply during bring-up. Battery, solar and conversion are
deferred until Phase 8 produces real consumption numbers.

## D9 — Front end: **20 dB pad + FM notch per chain, both swappable**

Order doesn't matter electrically — both are passive and neither can be overloaded.

The pad is a profile setting, not a constant. Twenty dB is right when everything is
within a few kilometres. Monitoring a distant repeater from a fixed location, that
same pad throws away signal you need.

## D10 — Antennas: **465 MHz half-wave whip + 2 m/70 cm dual-band whip**

**Gain doesn't matter here.** We're deliberately discarding 20 dB; buying gain buys
more of what we're throwing away. High-gain verticals are also narrower in bandwidth
and have a tighter vertical pattern, which risks missing close-in signals.

**A resonant antenna is a mild free filter.** Being the wrong length for FM broadcast
or cell means it delivers those poorly — maybe 6–10 dB of free help. A discone is
deliberately broadband and hands the front end everything at full strength, which is
the wrong direction for an overload-sensitive design. Worth owning as the
unknown-environment antenna, paired with more attenuation.

**Half-wave, not quarter-wave.** A quarter-wave needs a ground plane beneath it —
radials or metal. A half-wave is self-contained and doesn't care what it's mounted on.

**Watch SMA gender.** The Airspy's port is female, so antennas need a male plug —
usually sold as "for Yaesu/Kenwood/Icom." Baofeng-style antennas are female and won't
mate. Buy a couple of female-to-female adapters as insurance.

## D11 — Shared clock: **not needed**

Only matters for coherent work like direction finding, which we cut. Each Airspy's
TCXO is around ±0.5 ppm — about ±230 Hz at 466 MHz against 12.5 kHz channel spacing.
What you *do* need is to measure each receiver's actual offset once against the
MiniSA's generator and record it, so logged frequencies land on the right channel.

## D12a — Interface: **headless, monitor added later**

Headless over SSH, no desktop. Either **Ubuntu Server 24.04 LTS** or **Raspberry Pi
OS Lite** — both work, and familiarity should decide it. Ubuntu configures via
cloud-init (`/boot/firmware/user-data` and `network-config`) with SSH on by default;
Pi OS configures via the Raspberry Pi Imager settings panel.

Ubuntu's cost is that `vcgencmd` and `raspi-config` aren't there and most
Pi-specific forum answers assume Pi OS. Neither blocks anything: temperature reads
from `/sys/class/thermal/thermal_zone0/temp`, throttle state from
`/sys/devices/platform/soc/soc:firmware/get_throttled`, and `config.txt` lives at the
same path on both. `libraspberrypi-bin` provides `vcgencmd` if wanted.

No desktop competing for CPU either way, and it matches how the deck will run in the
field, so there is no migration later.

The one step that wants a display is checking the spectrum during bring-up. Three
headless answers, in order of preference:

1. **`survey_prototype.py --spectrum`** — captures a band, writes a PNG with
   spectrum and waterfall, prints the strongest channels as text. Works over any
   connection including slow WiFi, and the text alone answers most questions.
2. **SoapyRemote** (`soapysdr-module-remote`) — the Pi streams samples, SDR++ or
   gqrx runs on your laptop. Best interactive experience. Full rate is ~30 MB/s so
   it wants gigabit ethernet; drop to 2.5 MSPS to browse over WiFi.
3. **VNC** — works, but a waterfall over VNC is laggy.

**A USB-to-TTL serial adapter (~$8) is the headless equivalent of a monitor.** GND to
pin 6, TX to pin 10, RX to pin 8, `enable_uart=1` in config.txt. You will rarely need
it; the once you do — bad config, WiFi lockout, filesystem that won't mount — it is
the difference between a five-minute fix and pulling the card.

Adding a screen and keyboard later changes nothing already decided. Either keep it
headless and use the screen for a field status view, or `apt install
raspberrypi-ui-mods` onto the existing install and re-measure the Phase 2 CPU numbers.

## D12 — Cooling: **official Active Cooler**

Not optional. A Pi 5 under sustained four-core load throttles without it, and
throttling here shows up as lost samples rather than an error message.

---

## Core bill of materials

| Item | Qty | Cost |
|---|---|---|
| Airspy R2 | 2 | $338 |
| Raspberry Pi 5, 8 GB | 1 | $80 |
| Active Cooler | 1 | $5 |
| Official 27 W USB-C PSU | 1 | $12 |
| M.2 HAT+ | 1 | $12 |
| NVMe SSD 500 GB, **2242** | 1 | $45 |
| microSD 32 GB | 1 | $8 |
| USB-to-TTL serial adapter (headless console) | 1 | $8 |
| 465 MHz half-wave whip, SMA male | 1 | $25 |
| 2 m/70 cm dual-band whip | 1 | $25 |
| FM broadcast notch filter | 2 | $30 |
| SMA attenuators (2× 20 dB, 2× 10 dB) | 4 | $32 |
| SMA dummy load, adapters, jumpers | — | $48 |
| **Total** | | **$668** |

Split into two purchases — see `bench-bringup.md`.

---

## Known risks, honestly

| Risk | Confidence | Notes |
|---|---|---|
| Detection and logging | high | Several mature projects prove this works |
| Tone identification | high | Tested against synthetic signals, all 54 tones |
| Multichannel demod on a Pi | high | RTLSDR-Airband does exactly this in production |
| 24 h unattended | medium | Always harder than expected |
| Digital decoding | medium | op25 and dsd-fme are notoriously fiddly builds |
| **Repeater matching** | **~50%** | **No prior art found. The novel part.** |

**Repeater matching is the one to watch.** No existing project automatically pairs a
repeater's two frequencies and extracts its access tone, which is what makes tier T3
meaningful. It's testable on a bench with your radios — see Phase 7.

## Worth building on rather than writing

| Project | What it already does |
|---|---|
| **RTLSDR-Airband** | Many channels from one wideband radio, unattended daemon, runs on a Pi. Mature C, used for live aviation feeds. |
| **Trunk Recorder** | Wide capture → many simultaneous recordings → database |
| **shajen/rtl-sdr-scanner-cpp** | Band scanning, simultaneous recording, web interface |

The novel part of this project is tones-as-discovery, repeater matching, and the
contactability tiers. The channel-splitting is solved. Strongly consider building the
enrichment layer on top of RTLSDR-Airband rather than writing your own.

---

## Deliberately out of scope for now

Portable power · enclosure and sealing · screen and keyboard mounting · profiles
beyond the first · digital decoding of talkgroups and colour codes · direction finding
· HF · Meshtastic and WiFi survey · trunked system following.
