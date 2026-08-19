# Bench bring-up plan

Getting the Pi 5 and two Airspy R2s working on wall power, with real signals logged to a
database. **Headless — no monitor, keyboard or mouse.** Everything over SSH from your
laptop. No enclosure, no battery.

**Goal at the end:** a rig on your bench that watches 466 MHz, notices every time an FRS or
GMRS radio keys up, works out the tone, and writes it to SQLite — running unattended for 24
hours without you touching it.

Companions: `design-decisions.md` for why the hardware is what it is, `rf-primer.md` for the
radio concepts. `pi-architecture.md` for where things live on the Pi.

---

## Status

| Phase | State | Date | Result |
|---|---|---|---|
| **0** — Pi alone, no radio | **PASS** | 2026-08-18 | 28.8% of one core, 27 concurrent, peak 71.6 °C, zero throttling, fan confirmed |
| **1** — first radio | not started | | awaiting Airspy R2 |
| **2** — detection and logging | not started | | |
| **3** — tones | not started | | |
| **4** — 24 h single radio | not started | | |
| **5** — digital | not started | | |
| **6** — second radio | not started | | |
| **7** — repeater matching | not started | | |
| **8** — 24 h everything | not started | | |

### As-built, confirmed on hardware

| | |
|---|---|
| OS | Ubuntu 26.04 LTS, kernel 7.0.0-1016-raspi, aarch64 |
| Hostname | `radio-deck` |
| Boot | NVMe, `BOOT_ORDER=0xf146` (NVMe → USB → SD → restart) |
| Storage | WD Black SN770M 500 GB, 465 G root + 512 M `/boot/firmware` |
| PCIe | **Gen2 x1, 5 GT/s.** Drive is Gen4 x4 capable; the Pi's slot is Gen2 by default. Left as-is deliberately — ~450 MB/s against a workload needing single-digit MB/s. |
| RP1 link | Gen2 x4, ~2 GB/s, carries all USB. Not a constraint. |
| USB 2.0 buses | **Bus 002 and Bus 004**, `xhci-hcd/2p` each. One radio per bus. |
| Third USB bus | `Bus 001 dwc2/1p 480M` on the USB-C connector — see note below |
| Network | WiFi only, `eth0` down |
| Python | 3.14.4, numpy 2.3.5, scipy 1.16.3 |
| Watchdog | `dtparam=watchdog=on` already in config.txt |

### Findings that changed assumptions

**The Pi 5 is faster than estimated.** Predicted 35–40% of one core and 12–15 simultaneous
transmissions; measured 28.8% and 27. About 2.2× slower than the x86 reference, not 2.5–3×.
**Two radios project to ~57% of one core — 14% of the machine.** The second Airspy fits with
room, which was the open question from Phase 0.

**The throttle register is unavailable and it doesn't matter.** `vcgencmd` installs on Ubuntu
but `/dev/vcio` doesn't exist, so `get_throttled` fails. Irrelevant: CPU clock is the honest
measurement. It held 2400 MHz across all 292 samples under full load. Undervoltage alarm via
hwmon reads 0.

**Temperature verdict of "marginal" is over-cautious here.** 71.6 °C peak was `stress-ng`
pinning four cores at 100%. The real workload is 7–14%, so operating temperature will sit
much nearer the 44 °C idle. Still relevant later for a sealed case at 100 °F ambient, not
now.

**There is a third USB 2.0 controller.** `Bus 001` is a `dwc2` controller on the USB-C
connector. If it can be put into host mode it would allow a third radio without a PCIe card
— at the cost of powering the Pi through the GPIO header. Worth investigating after WildFire,
possibly cheaper and simpler than the PCIe route. Not now.

**NumPy is linked against reference BLAS, not OpenBLAS.** Low priority: the hot paths are FFT
(numpy's own pocketfft) and `upfirdn` (scipy), neither of which uses BLAS. Only the small tone
DFT matmul would benefit, and it isn't the bottleneck.

**Cooling confirmed working.** Fan reads 0 rpm at 44 °C idle (correct — the controller stops
it below ~50 °C) and **529 rpm under load at ~61 °C**, which is a low step on the fan curve.
So the Active Cooler is connected, the fan controller is responding to temperature, and there
are further steps available before it runs out of headroom. Combined with the clock holding
2400 MHz across all 292 samples, the script's "marginal" temperature verdict was conservatism
rather than a real finding. Thermal is not a concern on the bench; revisit only when the
enclosure is sealed.

---

---

## Headless is fine, and it's how it'll run anyway

Only one step in this plan genuinely wants a display: Phase 1, where you need to *look* at
the spectrum to confirm the RF chain is sane. There are three ways to do that without one,
covered in §Phase 1. The rest is command line and SQL.

Two things change from a monitor-attached build:

- **Install Raspberry Pi OS Lite, not Desktop.** Lighter, no compositor competing for CPU,
  and it's what you'd want in the field anyway — so no migration later. You can add a
  desktop afterwards with one `apt install` if you decide you want it.
- **Buy a USB-to-serial adapter.** About $8, and it's the headless equivalent of a monitor:
  console access even when networking has failed. Details below.

A section at the end covers what to do when you add a screen and keyboard later.

---

## Ground rules

**Buy in two tranches.** Everything through Phase 5 uses one radio.

| Tranche | Buy | Cost |
|---|---|---|
| **1** — Phases 0–5 | Pi kit (have it), SSD, case+HAT, one Airspy R2, RF bits | ~$260 remaining |
| **2** — Phases 6–8 | Second Airspy R2, second notch and pad | ~$190 |

**Phase 0 needs no radio at all.** Prove the computer, storage, cooling and the entire
signal-processing chain before the Airspy ships.

**Everything has a gate.** Don't move on until the current phase passes. When something
fails there should only be one thing it could be.

---

## Parts

### Already ordered

CanaKit Pi 5 Essentials Kit (8 GB) · GeeekPi metal case with M.2 HAT+ and Active Cooler ·
WD Black SN770M 500 GB M.2 2230

### Still to buy — tranche 1

| Search on Amazon | ~$ | Why |
|---|---|---|
| `SMA attenuator kit DC-6GHz 1dB 2dB 3dB 5dB 10dB 20dB` | 22 | The design philosophy in one box |
| `SMA male female adapter kit RF connector` | 15 | Nothing connects without it |
| `USB to TTL serial adapter CP2102 3.3V` | 8 | **Headless safety net** — console when the network won't |
| `SMA male telescopic antenna 25-1300MHz` | 10 | Adjustable; 32 cm for 465 MHz |
| `SMA 50 ohm dummy load terminator male` | 8 | Transmit into this, not the air |
| `RTL-SDR Blog Broadcast FM Block filter` | 15 | Front-end protection |
| `RG316 SMA male to SMA male cable 3ft` | 10 | Get a 2-pack |
| **`Airspy R2 SDR`** | 169 | Check price — over ~$180, buy from airspy.us |

**SMA gender:** the Airspy's port is female, so antennas need a male plug. The adapter kit
covers you either way.

---

## Phase 0 — the Pi on its own, no radio

### Build it

1. **Fit the Active Cooler first.** A Pi 5 under sustained load without it slows itself down,
   and that shows up later as lost samples rather than an obvious error.
2. Fit the M.2 HAT+ with its standoffs and ribbon. SN770M in the slot.
3. Assemble into the GeeekPi case.

### Flash it for headless boot

Two options. Pick whichever you'd rather live in — both work.

**Ubuntu Server 24.04 LTS (arm64)** — if you already know Ubuntu, the familiarity is worth
more than anything Pi OS gives you here.

- **Flash straight to the NVMe** via a USB-to-M.2 adapter. Skips the SD-then-clone dance
  entirely.
- SSH is on by default. Login is `ubuntu` / `ubuntu`, forced change on first login.
- Headless config is cloud-init, not the Imager gear icon: edit `/boot/firmware/user-data`
  for hostname, users and SSH keys, and `/boot/firmware/network-config` for WiFi. Both are
  plain YAML on the boot partition, readable from any machine.
- Use 24.04 LTS rather than the newest release. Pi 5 support has been settled there for a
  while, and this is a project where boring is good.

**Raspberry Pi OS Lite (64-bit)** — if you'd rather have every forum answer apply directly.
Use **Raspberry Pi Imager** and click the **gear icon before writing**: hostname, enable
SSH, username and password, WiFi SSID and country, locale. Skip any of those and you have a
Pi you cannot talk to.

**Either way, use ethernet for first boot if you possibly can.** WiFi config typos are the
single most common way a headless first boot fails, and ethernet removes the variable.

### What differs on Ubuntu

Nothing that blocks anything, but four things to know:

| Pi OS | Ubuntu |
|---|---|
| `vcgencmd measure_temp` | `cat /sys/class/thermal/thermal_zone0/temp` (millidegrees) |
| `vcgencmd get_throttled` | `cat /sys/devices/platform/soc/soc:firmware/get_throttled` |
| `raspi-config` menus | edit `/boot/firmware/config.txt` directly — same path |
| Boot order via raspi-config | `sudo rpi-eeprom-config --edit`, set `BOOT_ORDER=0xf416` |

`sudo apt install libraspberrypi-bin` gets you `vcgencmd` itself if you'd rather. Snapd runs
by default on Ubuntu Server; remove it if you like, it's irrelevant at 8 GB.

The commands below use the sysfs paths, which work on both.

### Find it and get in

```bash
ssh <user>@surveydeck.local          # mDNS, works on most networks
# if that fails, check your router's DHCP client list for the IP
```

### The serial console safety net

Wire the USB-TTL adapter to the Pi's GPIO: **GND→pin 6, TX→pin 10, RX→pin 8.** Do **not**
connect the adapter's power pin. Then on the SD card's boot partition, add to `config.txt`:

```
enable_uart=1
```

From your laptop: `screen /dev/ttyUSB0 115200` (or PuTTY on Windows).

You will probably never need it. The one time you do — a bad `config.txt`, a WiFi change
that locks you out, a filesystem that won't mount — it's the difference between a five-minute
fix and pulling the card to a laptop.

### Set it up

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y rpi-eeprom          # Ubuntu; already present on Pi OS
sudo rpi-eeprom-update -a
sudo reboot
```

Turn on the PCIe slot for the NVMe:

```bash
echo -e "dtparam=pciex1\ndtparam=pciex1_gen=3" | sudo tee -a /boot/firmware/config.txt
sudo reboot
lsblk                      # you want /dev/nvme0n1
```

If gen3 misbehaves, drop that line — gen2 is the officially supported speed and is plenty.

If you flashed straight to the NVMe you're already there. Otherwise set the EEPROM to try
NVMe first:

```bash
sudo rpi-eeprom-config --edit          # set BOOT_ORDER=0xf416
```

That value reads right-to-left: NVMe, then SD, then USB, then restart. It lives in the
EEPROM, so it's independent of which OS you install.

### Install the software

```bash
sudo apt install -y \
  airspy soapysdr-tools soapysdr-module-airspy python3-soapysdr \
  python3-numpy python3-scipy python3-matplotlib sqlite3 git stress-ng
```

No `gqrx` — that's a GUI app and there's no display. Phase 1 covers the alternatives.

### Heat test

```bash
bash deck-check.sh soak 10
```

Runs stress-ng on all four cores for ten minutes while sampling temperature and CPU clock
every two seconds, then reports peaks. **You don't have to watch it** — peaks are captured
and a CSV is written.

The important measurement is the **clock**, not a throttle register. A Pi 5 should hold
2400 MHz throughout. If the clock sags while temperature climbs, that *is* throttling,
whatever any bitmask says — and the clock is readable on every OS.

Throttle registers are checked too if the kernel exposes them, but on Ubuntu they often
aren't there. That's fine; you don't need them.

### Run the selftest — the valuable bit

```bash
python3 survey_prototype.py --selftest --rate 10e6
```

No radio needed. Generates synthetic FM with known tones, checks the detection logic against
them, and benchmarks the machine. About a minute.

For reference, on a mid-range x86 core — a Pi 5 should land roughly 2.5–3× slower:

```
  frame size        131072 samples = 13.1 ms  (76 frames/sec)
  channels watched  1594
  ring buffer       192 MB

  tone identification  (checked at 2.4 MSPS — logic is rate-independent)
    54/54 standard tones correct
    weak tone in noise: ok
    12/12 DCS codewords correctly rejected
    no-tone carrier: clean

  speed on this machine
    detection per frame     1.65 ms   ->  12.6% of one core
    background estimate     0.96 ms   ->   0.7% of one core
    analysing one event    100.6 ms   -> 13.9x realtime per core

    steady load, one radio: 13.3% of one core (3.3% of four)
    simultaneous transmissions, 4 cores: about 39
```

**On a Pi 5, expect roughly 35–40% of one core steady, and 12–15 simultaneous
transmissions.** Much worse than that and we should talk before you buy a radio.

### Gate 0

| Check | Pass |
|---|---|
| SSH in over the network | yes |
| Serial console works (test it once) | yes |
| Boots from NVMe | yes |
| Ten minutes at full CPU | under 70 °C |
| `get_throttled` (sysfs or vcgencmd) | `0x0` |
| `dmesg` after an hour idle | no undervoltage |
| Selftest correctness | all four checks pass |
| Selftest steady load | under 60% of one core |

**Send me the selftest output here.** Best single data point — your machine's real DSP speed
and whether the algorithms pass on your build, before hardware variables enter.

---

## Phase 1 — first radio, first signal

**Chain:** antenna → FM notch → 20 dB pad → Airspy → USB → Pi

Order of notch and pad doesn't matter electrically; both are passive.

### Does the Pi see it?

```bash
airspy_info
SoapySDRUtil --find="driver=airspy"        # note the serial number
SoapySDRUtil --probe="driver=airspy"       # note available sample rates
```

**Write the serial down.** Always address radios by serial, never index — index order changes
between reboots, and with two radios that silently swaps which band is which, with no error.

### Seeing the spectrum without a monitor

Three options. Use the first for gates, the second if you want to poke around.

**1. Built-in capture — works over any connection**

```bash
python3 survey_prototype.py --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain 12 \
  --spectrum band.png --spectrum-seconds 20
```

Writes a PNG with an averaged spectrum and a waterfall, and prints the strongest channels as
text right in your terminal:

```
  strongest channels (peak hold over 20 s):
     462.5625 MHz   + 32.6 dB
     464.5500 MHz   + 25.3 dB
     467.7000 MHz   + 18.0 dB
```

Then `scp <user>@surveydeck.local:band.png .` to look at it. The text alone answers most
questions; the PNG is for when something looks odd.

Note this uses a *band-wide* reference level rather than the rolling one the live detector
uses, deliberately — a carrier that never stops would otherwise be absorbed into its own
background and vanish.

**2. Live spectrum on your laptop — best for exploring**

```bash
sudo apt install -y soapysdr-module-remote      # on the Pi
SoapySDRServer --bind                            # runs it
```

Then on your laptop, point SDR++ or gqrx at `driver=remote,remote=surveydeck.local`. You get
a full-speed waterfall on a big screen, with the Pi just streaming samples.

Full rate is ~30 MB/s, which wants gigabit ethernet. Over WiFi, drop to 2.5 MSPS for
browsing — plenty for confirming a signal is where you think it is.

**3. VNC.** `sudo raspi-config` → Interface Options → VNC, then install a desktop. Works,
but a waterfall over VNC is laggy and it's the heaviest of the three.

### First signal

Key an FRS radio into the dummy load a few feet away, with `--spectrum` running. You should
see a carrier at 462.5625. Cycle channels 1–7 and confirm each lands where it should —
462.5625 through 462.7125, 25 kHz apart.

### Check the notch filter actually works

MiniSA generator → notch filter → analyser in. Sweep.

| Frequency | Want |
|---|---|
| 88–108 MHz | at least 30 dB down |
| 466 MHz | under 1.5 dB lost |

Filters at this price are sometimes not what the label claims.

### Set the gain, once, properly

1. Antenna off, dummy load on. Run `--spectrum`, note the band reference level it prints.
2. Antenna on. Raise gain until that level rises 8–10 dB.
3. Confirm the capture reports **zero clipping frames** while someone keys nearby.
4. **Write the number down.** Standing setting.

Airspy gain splits across three stages. Start in "linearity" mode around 8–12.

### Measure the frequency error

MiniSA generator at exactly 466.000 MHz. See where `--spectrum` puts the peak. Difference
divided by 466 gives parts-per-million. Record it — the software needs it so logged
frequencies land on the right channel. Expect under ±1 ppm, about ±470 Hz, against 12.5 kHz
channel spacing.

### Gate 1

| Check | Pass |
|---|---|
| Airspy enumerates, serial recorded | yes |
| FRS carrier at correct frequency | yes |
| All 7 channels land correctly | yes |
| Zero clipping frames at working gain | yes |
| Notch filter measured | yes |
| Gain and ppm recorded | yes |
| Nothing in the spectrum you can't explain | yes |

That last row matters. Anything unexpected — check against the MiniSA. If the analyser
doesn't see it in the air but the Airspy does, it's being manufactured inside the receiver
and you need more attenuation.

---

## Phase 2 — detection and logging

```bash
python3 survey_prototype.py \
  --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain 12 \
  --ppm <MEASURED> --db bench.sqlite --receiver-id uhf --stats
```

Run it under `tmux` or `screen` so it survives your SSH session dropping:

```bash
sudo apt install -y tmux
tmux new -s survey        # detach with ctrl-b then d, return with: tmux attach -t survey
```

### Test with your radios

| Test | Expect |
|---|---|
| Hold PTT 3 seconds | one event, right frequency, 3.0 ±0.15 s |
| Five 1 s presses, 2 s apart | five separate events |
| Five 1 s presses, 0.2 s apart | one event, not five |
| 300 ms quick press | detected |
| GMRS radio on a 462.5x channel | right channel |
| Watching `--stats` while keying | overflow count stays zero |

```sql
sqlite3 bench.sqlite "SELECT freq_hz/1e6, duration_s, peak_snr_db FROM events ORDER BY id DESC LIMIT 20;"
```

### Gate 2

| Check | Pass |
|---|---|
| Events within 1 kHz of true frequency | yes |
| Durations within 150 ms | yes |
| Zero overflows in an hour | yes |
| CPU across four cores | under 40% |
| Temperature | under 70 °C |

**This CPU number decides whether the second radio is worth buying.** On Lite there's no
desktop to skew it — just `htop` in another SSH session.

**Come back to me after this gate whether it passes or not.** If one radio at 10 MSPS is
already near 40%, two won't fit and we should optimise or move to the Radxa X4 before you
spend the second $169.

---

## Phase 3 — tones

Use the ham handheld; it can be set to any tone, unlike an FRS radio's fixed list. Transmit
into the dummy load at lowest power.

| Test | Expect |
|---|---|
| CTCSS 100.0 Hz | reports 100.0, capture ≥ 0.5 |
| 67.0 then 69.3 | tells them apart |
| 151.4 then 156.7 | tells them apart |
| Every tone the radio offers | all correct |
| **A DCS code** | flags DCS, does **not** report a tone |
| **DCS, inverted** | same |
| Carrier with no tone | reports nothing |
| 300 ms press | logged, tone may be absent — correct |
| Add the 10 dB pad | still correct |

**The DCS rows matter disproportionately.** CTCSS is a steady tone; DCS is a repeating
digital code. The obvious approach — "find the strongest frequency" — gets fooled by DCS
about three times in four. The code measures what *fraction* of the low-frequency energy
sits in one tone instead: real tones hold ~99%, DCS never got above 3% in testing.

Get this wrong and nothing breaks. You get a believable log where every DCS channel carries
a tone that won't open the squelch, and you find out in the desert.

### Gate 3

Every row behaves. The synthetic version already passes in `--selftest`; this confirms it
through a real signal path.

---

## Phase 4 — leave it running

Antenna somewhere with a view, parked on 466.0, 24 hours under tmux. You'll pick up real
traffic — neighbours, retail staff, construction crews, school buses.

Watch for memory flat over 24 hours, overflow count still zero, disk growth matching
projection, temperature stable, frequencies clustering on the real channel grid.

**Also your first honest look at whether the detection thresholds are right.** Too sensitive
and you'll wake to thousands of junk events; too blunt and you'll miss short transmissions.
Tune here, where you can listen on a handheld and compare.

### Gate 4

24 hours, no crash, no memory growth, no overflows, a channel list you believe.

---

## Phase 5 — digital

Transmit DMR into the dummy load, then analog FM from the same radio.

| | Analog FM | DMR |
|---|---|---|
| Frequency wiggle | smooth, continuous | **snaps between 4 fixed levels** |
| On/off pattern | continuous while keyed | 30 ms on, 30 ms off |

Add a histogram of the instantaneous frequency and you'll see it plainly — analog gives one
smooth hump, DMR four spikes. Cleanest distinction in the whole classifier.

At this stage DMR only needs to **not** be mistaken for analog and **not** produce a made-up
tone. Colour codes and talkgroups come much later.

### Gate 5

DMR classifies as digital, analog as analog, neither produces a false tone.

**Buy tranche 2 after this passes.**

---

## Phase 6 — second radio

```bash
lsusb -t
```

Each Airspy must be under a **different** `480M` root hub. The two USB 2.0 ports (the black
pair) are on separate controllers — simplest to use those. Both on one bus means lost
samples, and lost samples look like signals that were never there.

Two Airspys draw about 1 A against the Pi 5's 1.6 A budget:

```bash
dmesg -w | grep -i voltage
cat /sys/devices/platform/soc/soc:firmware/get_throttled
```

If undervoltage appears, add `usb_max_current_enable=1` to `/boot/firmware/config.txt`, or
fall back to a powered hub. Direct should work at two.

Radio 1 parked on 466.0, radio 2 rotating 446.0 / 146.0 / 153.2.

### The band-switching trap

The background estimator needs ~20 frames to settle. If it carries state across a retune you
get a burst of false detections after every band change — it reads the new band's different
background as a pile of signals appearing at once.

**Keep separate noise-floor history per band**, saved and restored on switching. Verify by
watching the log right after each change: no cluster of events at the boundary.

### Gate 6

Each radio on its own USB bus · six hours with zero overflows · no undervoltage · CPU under
75% · no event burst after band switches.

---

## Phase 7 — repeater matching

The part with no existing software to copy, and the part most likely to need rework. Which
is why it's worth testing where you control everything.

**Build a fake repeater with two of your radios.**

| Playing | Radio | Frequency | Tone | Timing |
|---|---|---|---|---|
| Someone talking to a repeater | GMRS | 467.700 | 141.3 Hz | key 3 s |
| The repeater answering | Ham HT | 462.700 | *different* tone, or none | start ~50 ms later, run ~1 s longer |

Both sit inside the parked 466.0 window, so one radio hears both halves. That's the whole
reason for that window.

The software should work out these are two events, 5 MHz apart, that 467.700 is the input
because it always starts first, that **141.3 came from the input side** and is the tone you'd
need to transmit, that the output's tone is a separate field, and that the channel is now
usable.

Then try to break it:

| Change | Checks |
|---|---|
| Repeater sends no tone | doesn't assume both tones match |
| Repeater sends a different tone | records both, doesn't overwrite |
| Input DCS, output CTCSS | handles mixed types |
| Delay 20 ms vs 200 ms | timing window wide enough |
| **Two unrelated channels 5 MHz apart** | **must NOT match them up** |
| Simplex only | doesn't invent a repeater |

That second-to-last row is the important one. Software that pairs everything 5 MHz apart is
worse than nothing — it would send you transmitting on a wrong frequency with a wrong tone.

### Gate 7

All rows behave, including the ones that should say no.

---

## Phase 8 — 24 hours, everything

Both radios, band switching, all classification, wall power, a full day. Log system health
alongside radio data — a cron writing temperature, CPU, free memory, disk and overflow count
to a text file is enough.

**Look for drift, not crashes.** Memory creeping, file handles leaking, disk filling faster
than rotation, a thread quietly dying. A clean crash at hour 19 is a *better* outcome than a
degradation you don't notice.

### Gate 8

24 hours. No restart, memory flat, temperature stable, disk within projection, zero
overflows, a channel list you'd program into a radio.

**When this passes the electronics and functionality are done.** Everything after — portable
power, enclosure, sealed-box cooling, mounting, screen and keyboard — is packaging around
something that already works.

---

## When you add a monitor and keyboard later

Nothing here needs redoing. Options, in order of how much they change:

**Keep it headless, add a screen for field use only.** The deck runs as a service; the screen
shows a status view over the local network or a text console. Least disruption, and it's what
the deployed design assumes.

**Add a desktop to the existing Lite install:**

```bash
sudo apt install -y raspberrypi-ui-mods lightdm
sudo raspi-config       # System Options -> Boot -> Desktop Autologin
```

Then `sudo apt install gqrx-sdr` if you want a local waterfall. Costs some CPU, so re-measure
your Phase 2 numbers afterwards, and consider booting to console for real runs.

**Sizing, when you get there:** a 7" 1024×600 HDMI IPS panel is the usual cyberdeck choice —
enough rows for a useful text interface, small enough to fit a case lid. A 40% mechanical
keyboard pairs with it. Neither affects anything decided so far.

---

## What to send me, and when

### The easy path

```bash
bash deck-check.sh all > phase0-$(date +%Y%m%d-%H%M).txt 2>&1
```

Run from the directory containing `survey_prototype.py`. Four modes:

| Command | Does |
|---|---|
| `bash deck-check.sh diag` | Collect everything, ~20 s |
| `bash deck-check.sh soak 10` | 10-minute load test, records peak temperature and clock |
| `bash deck-check.sh watch 60` | Sample for 60 min without adding load — use during a real run |
| `bash deck-check.sh all 10` | Soak then diag. This is the one to send. |

It finds the temperature sensor and throttle interface itself, so it works on Ubuntu and Pi
OS alike, and writes per-sample data to a timestamped CSV alongside the summary. Set
`DB=path` if your database isn't `bench.sqlite`.

That one file answers most of what I'd otherwise ask.

### What actually helps

1. **Raw output, pasted verbatim, in code blocks.** Not summarised, not retyped. Error
   messages complete with the stack trace. The exact text is usually where the answer is.
2. **Which phase and gate you're at.** Stops me re-diagnosing solved problems.
3. **What you expected versus what happened.** "Gate 2 wants under 40% CPU, I'm seeing 78%"
   beats ten paragraphs of description.
4. **The gate numbers even when they pass.** CPU, temperature, overflow count, ppm, the
   selftest benchmark. These let me predict whether the *next* phase works.
5. **The `--spectrum` PNG and its text output** for anything RF-related. Now that you're
   headless this replaces a gqrx screenshot, and it's better — the text peak list travels in
   a chat message on its own.

**What doesn't help:** paraphrased error messages, "it didn't work," or summaries of output
rather than the output. Not because I doubt you — the detail that turns out to matter is
almost never the one that seemed worth keeping.

### When to check in

| Point | Why |
|---|---|
| **Any failed gate** | Immediately. The gates exist so failures stay isolated. |
| **After Gate 0** | Send the selftest output. Best single data point. |
| **After Gate 2** | The CPU number decides whether two radios fit — before tranche 2. |
| **After Gate 5** | Last checkpoint before the second purchase. |
| **After Gate 8** | Everything works. Time to plan power and enclosure. |

### If you'd rather do one big dump at the end

Fine, and it works well if you keep a few things as you go. Between phases:

```bash
bash deck-check.sh all > phase0.txt 2>&1
```

Plus a plain running log:

```
PHASE 0  2026-08-20  PASS
  ssh + serial console both working
  temp under load: 62C   get_throttled: 0x0
  selftest: 54/54 tones, steady load 38% of one core, ~14 concurrent
  note: dropped pciex1_gen=3, gen2 works fine

PHASE 1  2026-08-24  PASS with question
  serial 0x1234ABCD   gain: linearity 10   ppm +0.6
  notch: 34 dB at 98 MHz, 0.9 dB at 466
  QUESTION: strong carrier at 464.550 that never stops. Real? band.png attached.

PHASE 2  2026-08-25  FAIL
  CPU 78% one radio, expected under 40%
  diag-phase2.txt attached
```

That plus the diagnostic files is everything I need, and you can work at your own pace.

### One thing about how this works

I don't retain anything between conversations, so a couple of lines of context when you come
back — "RF survey deck, Pi 5 plus two Airspy R2s, headless, at Phase 2" — plus the attached
files gets us straight to the problem. The four documents in this set are the shared
reference.

---

## Deliberately not here

Battery and solar · enclosure sealing and thermal design · screen and keyboard · profiles
beyond the first · digital decoding of talkgroups · direction finding.

---

## Cost and pacing

| Phase | Spend | Running total |
|---|---|---|
| 0 | $0 (parts on order) | — |
| 1 | ~$260 | ~$260 |
| 2–5 | $0 | ~$260 |
| 6 | ~$190 | ~$450 |
| 7–8 | $0 | ~$450 |

Six of nine phases cost nothing. The two purchases are separated by however long Phases 2
through 5 take, which is where most of the real work sits.
