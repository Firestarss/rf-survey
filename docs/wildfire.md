# WildFire field card

Everything needed at camp, in the order you need it. The survey starts by itself
at power-on — there is nothing to type to make it run.

## The two chains

Radios are addressed by serial, so it does not matter which USB port each goes
in, **but the pads do not move with the software** — the 5 dB radio and the
20 dB radio must not be swapped. Label them.

| role | Airspy serial | chain, antenna end first | profile |
|---|---|---|---|
| **UHF** | `…2e4c6dd7` | antenna → 2 dB → 3 dB → Airspy | 466 / 446 MHz, gain 42 |
| **VHF** | `…2f2f31d7` | antenna → **Flamingo** → 10 dB → 10 dB → Airspy | 146 / 154.95 MHz, gain 42 |

- One radio in **each black USB port** (they are separate USB controllers). Not the blue ports.
- Both antennas are 2 m / 70 cm dual-band, so either antenna works on either chain.
- Official 27 W PSU. A phone charger or power bank will limit USB current.

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

Once in:

```bash
systemctl is-active rfsurvey@uhf rfsurvey@vhf          # both: active
journalctl -u rfsurvey@uhf -n 400 | grep stats | tail -2 # fps, overflows, events
journalctl -u rfsurvey@vhf -n 400 | grep stats | tail -2
sudo ~/rfsurvey/systemd/rfsurvey-preflight              # boot checks
```

`overflow` counts dropped samples. Some are expected at 10 MSPS; a number that
climbs every line means that receiver cannot keep up.

## If something looks wrong

- **A receiver shows `failed`:** `sudo systemctl reset-failed rfsurvey@uhf && sudo systemctl start rfsurvey@uhf`
  (or `vhf`). It gives up after 30 failures in 30 minutes, so `failed` means
  something persistent — note it for later rather than fighting it.
- **Unreachable:** the survey does not need the network and is almost certainly
  still logging. Do not pull power just to regain access; that costs a run
  boundary and a misdated restart.

## Going home

Pulling the power at the end is fine — every event is committed as it is
written. `sudo poweroff` is tidier if you are connected.

Data comes home in `data/wildfire.sqlite` and `data/captures/wildfire/`.
