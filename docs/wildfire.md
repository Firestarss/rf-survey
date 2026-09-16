# WildFire field card

Everything needed at camp, in the order you need it. The survey starts by itself
at power-on — there is nothing to type to make it run.

## The two chains — Nagoya NA-771 on both

**Which radio is which:** tonight the UHF Airspy is the one with the **Signal
Stick** on it, and the VHF Airspy is the one with the **dummy load**. Label them
now, before anything is unscrewed. The software finds each radio by serial, so it
does not matter which USB port each goes in — but the attenuation is in the cable
run, not the software, and swapping the radios puts 20 dB on UHF and 5 dB on VHF.

| | UHF | VHF |
|---|---|---|
| Airspy serial | `637862dc2e4c6dd7` | `637862dc2f2f31d7` |
| has on it tonight | Signal Stick | dummy load |
| listens to | 466.000 (300 s) and 446.000 (60 s) MHz | 146.000 and 154.950 MHz, 180 s each |
| attenuation | **5 dB** (2 dB + 3 dB) | **20 dB** (10 dB + 10 dB) |
| FM notch | none | **Flamingo** |

**Full path, antenna end first:**

```
UHF   NA-771 → antenna adaptor → 2 dB pad → 3 dB pad → SMA cable → Airspy ...6dd7 → USB → black USB port
VHF   NA-771 → antenna adaptor → Flamingo → 10 dB pad → 10 dB pad → SMA cable → Airspy ...31d7 → USB → other black USB port
```

**Changes from tonight's setup:**

UHF radio
1. Unscrew the Signal Stick.
2. Take the Flamingo out of this chain; the adaptor now screws straight onto the 2 dB pad.
3. Fit the NA-771.

VHF radio
1. Unscrew the dummy load. **Keep it** — the antenna measurement needs it.
2. Build adaptor → Flamingo (moved from UHF) → 10 dB → 10 dB → SMA cable onto the Airspy.
3. Fit the NA-771.

You need **a second antenna adaptor** for the second chain, the same kind the
Signal Stick uses if the NA-771s have the same connector. The two 10 dB pads are
the ones taken off before the second propagation walk.

- One radio in **each black USB port** — they are separate USB controllers. Not the blue ports.
- Keep the two antennas **apart**: a metre if you can, and never touching.
- Official 27 W PSU.

### Measure after swapping antennas — this is not optional

The working gain belongs to the antenna, not the radio: swapping NA-701 for
Signal Stick moved it from 39 to 42 on 2026-09-16. Neither chain has been
measured with an NA-771, and VHF's gain of 42 was never measured at all. So once
both are built:

```
deck  →  10  Measure antenna & attenuation
```

Once per radio. It stops that receiver, asks you to fit the antenna and then the
dummy load, reports the lowest gain that is still linear and the attenuation it
would choose, offers to write the gain into the profile, and reminds you to put
the antenna back.

## Before leaving

The deck soaks overnight under its real WildFire configuration. In the morning,
connected over SSH or Tailscale:

```bash
sudo ~/rfsurvey/tools/wildfire-ready
```

It stops the survey cleanly, reports how the soak went, moves the soak's data
aside so WildFire starts with an empty database, checks both radios are attached
and the boot checks pass, then powers off. If anything looks wrong it says so
and does **not** power off. `--dry-run` reports without changing anything.

## The engine

Both receivers run at 10 MSPS on the **rust engine** (`--engine rust`, set in
`systemd/rfsurvey-<rx>.env`). Measured 2026-09-16 on this profile, both radios:

| | Python engine | rust engine |
|---|---|---|
| overflows, 6.5 min | 2,177 and 1,899 | **0 and 0** |
| frames/s (need 153) | 64-132 | 152.6 |
| load / temperature | 5.65 / 71.6 C rising | 2-3 / ~65 C |

Under heavy traffic a small share of events are logged without tone analysis
(8% at Boston's 22,000 events/hour); at a camp that should be close to none.

**Fallback**, if the morning report shows restarts or overflows: in both
`systemd/rfsurvey-uhf.env` and `systemd/rfsurvey-vhf.env` replace
`--engine rust` with `--engine python --profile profiles/wildfire-fallback.yaml`.
That keeps uhf at 10 MSPS and runs vhf at 2.5 MSPS over five narrower windows,
measured at zero vhf overflows.

## Running it: `deck`

SSH in (Blink or Termius on the iPad, any terminal on a laptop) and type `deck`.
Numbered menu, nothing to remember:

```
 1 Live status          6 Busiest channels          11 Boot checks
 2 Start survey         7 Change profile            12 Web dashboard
 3 Stop survey          8 Change engine             13 Start at boot
 4 Restart survey       9 New run / database        14 Logs
 5 Recent events (live) 10 Measure antenna (padcal) 15 WildFire ready / reboot / power off
```

`deck status`, `deck events 50` and `deck channels 60` work without the menu.
Everything the menu changes is a line in `systemd/rfsurvey.env`.

## Watching it: the dashboard

A web page served by the deck itself: receiver state, whether each radio is
keeping up, events per minute over the last hour, recent events with channel
names and tones, and the busiest channels. Self-contained — no internet needed.

```
http://<deck address>:8080        same network as the deck
http://100.82.163.31:8080         over Tailscale, where there is internet
http://radio-deck.local:8080      by name, if the network allows it
```

Start, stop or enable it at boot from `deck → 12`. It shows the deck's live data
and needs no login, so anyone on the same network can open it — fine on your
hotspot, worth a thought on the lodge WiFi.

Measured 2026-09-16 with both radios surveying at 10 MSPS under ~22,000
events/hour, the dashboard pointed at a 1.2-million-event database and three
simulated browsers polling every endpoint once a second for four minutes:
**zero overflows on either radio**. It runs in the idle scheduling class, and its
expensive queries are cached for 30-60 s however many browsers are open.

**Getting a page to the iPad at camp**, best first:

1. **Your hotspot.** iPad and deck both join `What iPhone?`; open the deck's
   address. Works anywhere you have the phone, internet or not.
2. **Tailscale**, if the lodge WiFi or the hotspot has internet.
3. **The lodge WiFi directly** — only if it lets devices see each other, which
   public WiFi often does not.

Longer term, the robust answer is the deck broadcasting **its own WiFi network**
for the iPad to join — no infrastructure at all. It needs a USB WiFi adapter so
the Pi's built-in radio can stay a client; not attempted tonight.

## At camp

1. Antennas on, both chains assembled, radios in the black ports.
2. Plug in power.
3. **Write down the time you plugged it in, to the minute.** See *The clock*.
4. Walk away. The green activity LED flickering constantly is the survey writing.

If power is ever lost and comes back, the survey restarts by itself — **note
that time too.**

## The clock

The Pi has no RTC battery, so after any power cut it believes it is
**2026-07-27**, and with no internet nothing corrects it. The data is still good:
a run can be corrected exactly afterwards from one known real time, as was done
for six runs on 2026-09-16. That is why you note the time.

If the deck ever reaches the internet — the lodge WiFi, or your hotspot with
cell data — chrony fixes the clock by itself and logs exactly how far off it
was, which also makes the correction automatic. Notes are still the fallback.

## Checking on it

Known networks: **`jnwwifi`** (the lodge) and **`What iPhone?`** (your hotspot).

- **On your hotspot:** turn it on near the deck; the Pi joins within a minute or
  two. If the phone has cell data, `ssh firestarss@100.82.163.31` (Tailscale)
  works. Without data: an iPhone hotspot always hands out `172.20.10.2`–`.14`
  (the phone is `.1`) and does not list clients, so try
  `ssh firestarss@radio-deck.local` first, then those addresses.
- **In range of the lodge:** Tailscale works if the lodge WiFi reaches the
  internet. Direct connections between devices on public WiFi are usually
  blocked, so use the Tailscale address.

Once in, `deck` — option 1 for live status. Or open the dashboard (above).

`overflow` counts dropped samples. A number that climbs between refreshes means
that receiver cannot keep up.

## If something looks wrong

- **A receiver shows `failed`:** `deck → 14` for its log, then
  `sudo systemctl reset-failed rfsurvey@uhf` (or `vhf`) and `deck → 2`. It gives
  up after 30 failures in 30 minutes, so `failed` means something persistent —
  note it for later rather than fighting it.
- **Unreachable:** the survey does not need the network and is almost certainly
  still logging. Do not pull power just to regain access; that costs a run
  boundary and a misdated restart.

## Going home

Pulling the power at the end is fine — every event is committed as it is
written. `sudo poweroff` is tidier if you are connected.

Data comes home in `data/wildfire.sqlite` and `data/captures/wildfire/`, or
wherever `deck → 9` pointed it. Browse any past run on the dashboard with
`python3 tools/dashboard.py --port 8081 --db data/<run>.sqlite`.
