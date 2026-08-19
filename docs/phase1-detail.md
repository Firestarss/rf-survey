# Phase 1 — first radio, first signal (detailed)

Expanded from `bench-bringup.md` §Phase 1. Nothing here contradicts the plan; it's the same
gate with the steps broken out and the gqrx/desktop assumptions removed, since you're on
headless Ubuntu Server.

**What Phase 1 proves:** the Airspy works, it's plugged in somewhere sensible, signals land
on the frequency they claim to, your filters do what the label says, and you have three
numbers written down (serial, gain, ppm) that every later phase depends on.

**Time:** 2–3 hours if nothing surprises you. Steps 9–12 are the slow ones.

---

## Step 0 — bench inventory

Lay this out before you start. If anything is missing, note which steps it blocks.

| Item | Blocks |
|---|---|
| Airspy R2 + its USB cable | everything |
| SMA 50 Ω dummy load | steps 5–7, 11 |
| FM broadcast notch filter | step 9 |
| 20 dB SMA pads (x2) | steps 10–12 |
| 10 dB SMA pad | step 11 if you need finer adjustment |
| 465 MHz whip, SMA male | steps 11, 13 |
| SMA jumpers + F-F adapters | steps 9, 10 |
| FRS or GMRS handheld | steps 7, 8 |
| MiniSA (analyser + generator) | steps 9, 10, 12, 13 |

**Steps 1–8 only need the Airspy, the dummy load and a handheld.** If the RF accessories
haven't arrived, do those and stop at the gate.

---

## Step 1 — packages

The plan's list assumed Raspberry Pi OS. On Ubuntu Server, drop `gqrx-sdr` (it wants a
desktop) and add `matplotlib`, which `--spectrum` needs to write the PNG.

```bash
sudo apt update
sudo apt install -y \
  airspy soapysdr-tools soapysdr-module-airspy python3-soapysdr \
  python3-numpy python3-scipy python3-matplotlib sqlite3 git
```

Confirm the driver module actually registered:

```bash
SoapySDRUtil --info | grep -iA5 'available factories'
```

You want `airspy` in that list. If it's absent, the module package didn't install against the
SoapySDR version you have — stop and send me the output of `apt policy soapysdr-module-airspy`
and `SoapySDRUtil --info`.

---

## Step 2 — USB permissions

The airspy packages install a udev rule that grants access via the `plugdev` group.

```bash
id
```

Want `plugdev` in the group list. If it isn't there:

```bash
sudo usermod -aG plugdev $USER
```

Then log out and back in — group changes don't apply to an existing session. Verify with `id`
again before continuing.

**Do not work around this with `sudo`.** It'll appear to work now and then bite you in Phase 8
when the thing runs as a service under a non-root user.

---

## Step 3 — plug it in

**Antenna port first: screw the dummy load onto the Airspy before you plug anything in.** A
bare SMA jack on a sensitive receiver, with a 5 W handheld somewhere on the same bench, is how
front ends die. Get in the habit now while it's cheap.

Plug the Airspy directly into one of the **blue USB 3 ports**. No hub, no extension.

```bash
lsusb | grep -i airspy
lsusb -t
dmesg | tail -20
```

**Record which bus and port it landed on.** In Phase 6 the second radio has to go on the other
bus, and `lsusb -t` is how you'll confirm it. Note the line now while there's only one device
and it's unambiguous.

`dmesg` should show a clean high-speed enumeration and nothing else. Any reset loop, or
`device descriptor read` errors, means the cable or the port — try the other blue one before
suspecting the radio.

---

## Step 4 — enumerate and record the serial

```bash
airspy_info
SoapySDRUtil --find="driver=airspy"
SoapySDRUtil --probe="driver=airspy" | tee airspy-probe.txt
```

**Only one process can hold the radio at a time.** If `airspy_info` reports no devices
immediately after a SoapySDR command, something hasn't exited yet. `pgrep -a -f -i soapy` will
show you.

Write down, in a file you'll still have in six months:

- **Serial number** — from `--find`. From here on, address the radio by serial, never by index.
  Index order changes across reboots, and with two radios that silently swaps which band is
  which, with no error anywhere.
- **Firmware version** — from `airspy_info`.
- **Supported sample rates** — from `--probe`. Expect 10 MSPS and 2.5 MSPS.

Keep `airspy-probe.txt`. It's the reference for what gain names and ranges the driver exposes,
which comes up again in step 11.

---

## Step 5 — build the chain, dummy load first

Full chain, once you have the parts:

```
antenna → FM notch → 20 dB pad → Airspy
```

Both are passive, so the order of notch and pad doesn't matter electrically. Put them in
whatever order makes the cabling tidiest.

For now, **dummy load in place of the antenna**, everything else connected.

Hand-tight on SMA. Snug, then a nudge. These are brass and you can strip them with a wrench.

---

## Step 6 — baseline capture, nothing but the dummy load

```bash
cd ~/rf-deck        # wherever survey_prototype.py lives

python3 survey_prototype.py --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain 12 \
  --spectrum baseline-dummy.png --spectrum-seconds 20
```

**Record the band reference level it prints.** That's your "quiet" number and everything in
step 11 is measured against it.

The peak list should be nearly empty, or show only things a few dB above the floor. **Anything
strong here, with a dummy load on, is being generated inside your own box** — USB, the NVMe,
the Pi's switching regulators, or leakage into a cable. Write those frequencies down. They'll
be in every capture you ever take, and you want to recognise them rather than chase them
later.

Pull the PNG over when you want to look:

```bash
scp <user>@<pi>:~/rf-deck/baseline-dummy.png .
```

---

## Step 7 — first real signal

Dummy load still on. FRS handheld on channel 1 (462.5625), **low power**, six to ten feet away.

Enough energy leaks past a dummy load to give you a clear signal from a transmitter that close.
That's exactly what you want for a first test: it proves the whole receive path with zero risk
to the front end.

```bash
python3 survey_prototype.py --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain 12 \
  --spectrum first-signal.png --spectrum-seconds 20
```

Key the radio for about 5 seconds, twice, during the capture window.

**Pass:** the peak list names 462.5625 MHz. Not 462.55, not 462.57.

If the frequency is off by more than a few kHz, don't fix it yet — that's what step 12 is for.
If it's off by hundreds of kHz or lands somewhere unrelated, stop and send me the peak list.

---

## Step 8 — channel sweep

One long capture, cycling channels during it, is easier than seven separate runs. The spectrum
mode uses peak-hold, so everything you key shows up in the same list.

```bash
python3 survey_prototype.py --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain 12 \
  --spectrum channels.png --spectrum-seconds 90
```

During those 90 seconds, key ~8 s on each of channels 1–7, changing channel between keyings.

Expected in the peak list:

| Ch | MHz |
|---|---|
| 1 | 462.5625 |
| 2 | 462.5875 |
| 3 | 462.6125 |
| 4 | 462.6375 |
| 5 | 462.6625 |
| 6 | 462.6875 |
| 7 | 462.7125 |

25 kHz apart, evenly. **A consistent offset across all seven is a clock error and step 12
handles it. Uneven spacing is a sample-rate problem** and is much more serious — send me the
list.

---

## Step 9 — measure the notch filter

This is the step people skip and regret. Filters at this price are sometimes not what the
label claims.

**Measure a reference first.** Without it you're measuring your cables as well as the filter.

1. MiniSA generator out → jumper → straight into analyser in. Sweep 80–500 MHz. **This trace
   is your 0 dB reference** — save it or note the level at 98 MHz and at 466 MHz.
2. Now insert the notch in the middle of that same path. Sweep again.
3. Subtract.

| Frequency | Want |
|---|---|
| 88–108 MHz | at least 30 dB below reference |
| 466 MHz | no more than 1.5 dB below reference |

If it fails the 88–108 requirement, the filter is doing nothing useful and you'll find out
the hard way when a broadcast tower quietly ruins a run. If it costs you more than 1.5 dB at
466, it's eating the signal you actually want.

---

## Step 10 — sanity-check the pads

Same reference-then-insert method as step 9. Five minutes.

**Measure both 20 dB pads separately, and label them.** Masking tape, "A" and "B". This matters
more than it looks — see below.

For each pad, insert it into the reference path and sweep. Record attenuation at 150 MHz and at
470 MHz.

| Check | Want |
|---|---|
| Attenuation at 466 MHz | ~20 dB |
| Flatness, 150 vs 470 MHz | within a few tenths of a dB |
| Difference between pad A and pad B | record it, whatever it is |

**Why flatness matters:** cheap pads sometimes roll off badly at the top of their range, and a
pad that's 20 dB at VHF and 14 dB at UHF will quietly wreck your level assumptions later.

**Why the A-vs-B difference matters, and this is the one people skip.** Each receiver gets its
own permanent pad. If pad A measures 19.9 dB and pad B measures 20.3 dB, receiver 2 will report
every signal 0.4 dB weaker than receiver 1 would for the identical transmission — forever, in
every row of your database. That's a systematic offset, not noise, and it silently corrupts any
comparison between what the two receivers heard.

Two identical pads from the same batch should be close. But "should be" isn't a measurement.
Write both numbers down; the difference becomes a calibration constant you apply in software
rather than a mystery you chase in Phase 6.

Do the same for the 10 dB pad if it ends up in the chain.

---

## Step 11 — set the gain, once

The goal in plain terms: turn the gain up until the antenna's own noise is what you're
hearing, rather than the receiver's — but not so far that a nearby radio overloads the thing.
The number that tells you you're there is **8–10 dB**.

1. **Dummy load on.** Run `--spectrum` at gain 12. Note the band reference level. Call it
   `N_dummy`. (You have this from step 6.)
2. **Swap the dummy load for the antenna**, full chain, same gain. Run again. Call it `N_ant`.
3. Compute `N_ant − N_dummy`.
   - **8–10 dB → done.**
   - Less than 8 → raise gain one step, repeat from 1. Both numbers have to be re-measured at
     the new gain.
   - More than 10 → lower gain one step, repeat.
4. At the final gain, have someone key a handheld nearby while a capture runs. **Confirm zero
   clipping frames reported.** If it clips, you need more attenuation, not less gain — that's
   what the 10 dB pad is for.
5. **Write the number down.** This is a standing setting. Don't fiddle with it between runs or
   nothing you log will be comparable.

Airspy gain is split across three internal stages, and the driver exposes an overall
"linearity" setting from 0 to 21. Check `airspy-probe.txt` from step 4 for the exact names.
Starting at 12 and moving in single steps is right.

---

## Step 12 — measure the frequency error

MiniSA generator at exactly 466.000000 MHz, **through the 20 dB pad**, direct into the Airspy.
Capture, read where the peak actually lands.

```
ppm = (measured_Hz − 466000000) / 466
```

So a peak at 466.000420 MHz is +420 Hz, which is +0.9 ppm.

Expect under ±1 ppm. That's about ±470 Hz here, against 12.5 kHz channel spacing — comfortable.

**One caveat worth taking seriously:** the MiniSA's own reference oscillator has error too, and
on an inexpensive analyser it's plausibly worse than the Airspy's. So this measurement gives
you the *difference between two clocks*, not the Airspy's absolute error.

If you get a number under about 2 ppm, use it and move on — it's within tolerance either way.
If it comes out large, suspect the generator before the radio, and cross-check against
something you don't own: the NOAA weather transmitters at 162.400–162.550 MHz are commercial
gear held to tight tolerance and are a useful free second opinion. Tune there, capture, and see
whether the offset agrees.

**Record the ppm.** It goes into `--ppm` in Phase 2 and every phase after.

---

## Step 13 — account for everything in the spectrum

The last Gate 1 row, and the one worth actually doing rather than ticking.

Antenna on, full chain, working gain from step 11:

```bash
python3 survey_prototype.py --driver airspy --serial <SERIAL> \
  --freq 466.0e6 --rate 10e6 --gain <YOURS> \
  --spectrum survey.png --spectrum-seconds 300
```

Go through the peak list and put a name to every entry.

| What you'll probably see | What it is |
|---|---|
| 462.5625–462.7125 | FRS channels 1–7 |
| 462.550–462.725 (the .5x50 ones) | GMRS repeater outputs |
| 467.5625–467.7125 | FRS 8–14 |
| 464.500, 464.550, 469.500, 469.550 | common itinerant business — very likely near you |
| Anything else, unmoving, never modulated | suspicious |

**The test for a suspicious signal:** can the MiniSA see it on its own antenna? If yes, it's
real and it's in the air. If the MiniSA sees nothing but the Airspy shows it clearly, **it's
being manufactured inside your receiver** and the fix is more attenuation, not more filtering.

Compare against `baseline-dummy.png` from step 6 — anything present in both is internal by
definition.

---

## Gate 1

| Check | Pass | Yours |
|---|---|---|
| Airspy enumerates, serial recorded | yes | |
| USB bus/port noted | yes | |
| FRS carrier at correct frequency | yes | |
| All 7 channels land correctly, evenly spaced | yes | |
| Notch measured: ≥30 dB at 88–108 | yes | |
| Notch measured: ≤1.5 dB at 466 | yes | |
| Both 20 dB pads measured and labelled A/B | yes | |
| Pads flat across VHF/UHF | yes | |
| Pad A vs pad B difference recorded | yes | |
| Antenna-vs-dummy delta 8–10 dB | yes | |
| Zero clipping frames at working gain | yes | |
| ppm recorded | yes | |
| Nothing in the spectrum you can't explain | yes | |

### The numbers to keep

Everything downstream reads these. Put them somewhere permanent, not in shell history.

```
SERIAL = 0x................      # receiver 1
GAIN   = ..
PPM    = +/- ...
PAD_A  = ....  dB at 466 MHz
PAD_B  = ....  dB at 466 MHz     # measured now, used from Phase 6
```

---

## Log entry template

```
PHASE 1  2026-__-__  PASS / FAIL
  serial: 0x................   usb: bus _ port _
  gain: __ (linearity)   antenna-vs-dummy delta: __ dB
  ppm: ____
  notch: __ dB at 98 MHz, __ dB at 466 MHz
  pad A: __ dB at 150, __ dB at 470
  pad B: __ dB at 150, __ dB at 470   (A-B delta: __ dB)
  channels 1-7: all correct / notes
  unexplained: none / list
```

---

## Leftover from Phase 0

Gate 0 flagged one thing that never got resolved: **fan RPM read 0**, which is correct
behaviour below ~50 °C but was sampled at idle, so it doesn't prove the Active Cooler is
connected. 71.6 °C peak is warm for an actively-cooled Pi 5.

Cheap to settle while you're at the bench — start a load, then check:

```bash
stress-ng --cpu 4 --timeout 120s &
sleep 60
cat /sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input
cat /sys/class/thermal/thermal_zone0/temp
```

Non-zero RPM at temperature means the fan is fine and the metal case is just restricting
airflow. Zero RPM at 65 °C+ means it isn't plugged in, and that's worth knowing before you
start caring about thermal headroom in an enclosure.

---

## When to stop and send me something

| Symptom | Why it matters |
|---|---|
| `airspy` missing from SoapySDR factories | packaging mismatch, blocks everything |
| USB reset loop in `dmesg` | cable, port, or power — diagnose before blaming the radio |
| Channels unevenly spaced | sample-rate problem, not a clock offset |
| Strong peaks with the dummy load on | internally generated, changes the attenuation plan |
| Clipping that more attenuation doesn't fix | possible front-end damage |
| ppm above ±2 with the NOAA cross-check agreeing | worth understanding before Phase 2 |

Send the peak-list text plus `bash collect-diag.sh > diag-phase1.txt`. The text list travels
fine in a message on its own; attach the PNG only when something looks odd.
