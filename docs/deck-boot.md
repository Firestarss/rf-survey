# Boot configuration on `radio-deck`

Everything here exists because a correct diagnosis and a correct fix still
produced weeks of failure, for want of anything checking that the fix had taken
effect. The rule this document encodes:

> **Verify at the point of effect, not the point of edit.**
> For a kernel parameter that means `/proc/cmdline`. Never the file you edited.

---

## There are three files called `cmdline.txt` and two of them do nothing

Ubuntu images this machine with an A/B boot scheme. `config.txt` carries:

```
[all]
os_prefix=current/

[tryboot]
os_prefix=new/
```

so the firmware resolves the `cmdline=cmdline.txt` directive **relative to the
slot prefix**:

| path | read by the firmware? |
|---|---|
| `/boot/firmware/current/cmdline.txt` | **yes — this is the live one** |
| `/boot/firmware/new/cmdline.txt` | only during a tryboot |
| `/boot/firmware/cmdline.txt` | **no** |
| `/boot/firmware/cmdline.txt.bak` | **no** |

Three sessions in a row edited the top-level file. The giveaway was available
the whole time and nobody looked: that file also contains
`cfg80211.ieee80211_regdom=US` and `ds=nocloud;i=rpi-imager-…`, and **none of
those ever appeared in `/proc/cmdline` either.** The firmware was ignoring the
entire file, not just the parameter added to it.

The two inert files were renamed `*.INERT-DO-NOT-EDIT` on 2026-09-16, with a
`README.INERT-FILES` beside them. Renamed rather than deleted — recovery is one
`mv` if this reading is ever proved wrong.

**A consequence worth knowing:** `fsck.mode=force fsck.repair=yes` sat in the
inert file for weeks, so **no forced fsck has ever run on this machine.**

---

## The NVMe APST fix

```
nvme_core.default_ps_max_latency_us=0
```

Disables Autonomous Power State Transition on the NVMe. Without it the drive
enters a low-power state it does not reliably come out of; commands stop
completing, and the machine presents as: **ping answers, SSH accepts the
connection and then stalls before the banner, journald stops writing, already-
running processes keep going.** Anything needing a disk write dies; anything
purely in-kernel or already resident survives. That is why the survey kept
logging to its ring buffer while `sshd` was unreachable.

**The drive's own error log corroborates it.** Eleven unsafe shutdowns and a
filesystem abort with `num_err_log_entries = 0` and `media_errors = 0` means the
commands never reached the controller. A drive with a real hardware fault
records the fault.

Live since 2026-09-16 06:11 UTC. Verify with:

```bash
grep -o 'nvme_core[^ ]*' /proc/cmdline
```

---

## Kernel updates do NOT drop the parameter

This was believed to be a durability problem needing a postinst hook. It is not,
and the belief came from one confounded observation.

`flash-kernel`'s `pi-try` branch stages an update by **copying the live slot's
command line forward** — `/usr/share/flash-kernel/functions:1261`:

```sh
cp "$boot_mnt"/current/cmdline.txt "$boot_mnt"/new/cmdline.txt
```

Unconditional. So the parameter propagates by itself.

What actually happened on 2026-09-16, from file mtimes:

```
04:59:44   unattended-upgrades installs kernel 1017; flash-kernel stages new/
           and copies current/cmdline.txt AS IT WAS THEN — no APST parameter,
           because every previous edit had gone to the inert top-level file
06:03:56   session edits current/cmdline.txt (still the 1016 slot), adds APST
  reboot   tryboot promotes new/ (1017, cmdline snapshotted at 04:59) to
           current/, and demotes the slot edited at 06:03 to old/
           -> /proc/cmdline has no APST, and it looks like the update ate it
06:11:14   re-applied to the new current/, rebooted, confirmed
```

The parameter was not dropped by the update. **It was copied forward from a
snapshot taken 64 minutes before the edit existed.**

### The real hazard, which is narrower

`flash-kernel` copies `cmdline.txt` at **staging** time, not at reboot time. So:

> An edit to `current/cmdline.txt` made while a `new/` slot already exists never
> reaches `new/`, and the next reboot promotes `new/` over the edit.

`unattended-upgrades` installs kernels on this machine automatically, so a slot
can be staged at any hour without anyone typing anything. **After editing
`current/cmdline.txt`, always check whether `/boot/firmware/new/` exists**, and
patch it too if it does.

---

## The preflight check

`systemd/rfsurvey-preflight`, run by `rfsurvey-preflight.service` at boot and
ordered before `rfsurvey.target`, so its verdict sits in the journal immediately
above the survey's first output. Reading back from a lockup, you see the state
of the machine before you see the data.

It asserts:

1. **the parameter is in `/proc/cmdline`** — is the fix active on this boot
2. **the parameter is in `current/cmdline.txt`** — will it survive the next one
3. **no `new/` slot is staged without it** — the narrow hazard above
4. **NVMe SMART counters**, logged each boot as a baseline

Checks 1 and 2 are both needed and neither substitutes for the other. (1) alone
misses a live-but-not-persisted fix that vanishes at the next update; (2) alone
is exactly the mistake that caused all of this.

It **reports and does not gate**: a survey that runs and might hang still
produces data, and the deck is reachable over Tailscale to act on a warning. To
make a failed check stop the survey instead, set `RFSURVEY_PREFLIGHT_STRICT=1`
in the unit and add `Requires=rfsurvey-preflight.service` to `rfsurvey@.service`.

Run it by hand any time: `sudo systemd/rfsurvey-preflight`

---

## SMART baseline, 2026-09-16

```
Unsafe Shutdowns:                 11      <- the recurrence indicator
Media and Data Integrity Errors:   0
Error Information Log Entries:     0
Percentage Used:                   0%
Temperature (composite):          50 C
Temperature Sensor 1:             63 C    <- watch under Black Rock ambient
```

**`unsafe_shutdowns` alone cannot tell a lockup from a power pull**, and that was
learned the same day. It read 8 on 2026-08-31 and 11 on the morning of
2026-09-16, then **13 by that afternoon — with APST already disabled.** Taken at
face value that says APST was not the whole story. It does not say that:

| boot ended | journal ran to the end? | survey at the end | shutdown sequence |
|---|---|---|---|
| 06:43:31 | yes, to the final second | 76.3 fps, overflow 0 | none |
| 08:15:46 | yes, to the final second | 76.3 fps, overflow 0 | none |

Both machines were healthy and writing at the instant power disappeared. The
APST fault looks different: **journald goes silent hours before the machine
becomes unreachable**, while the survey keeps running.

So the recurrence test is two-part. A new unsafe shutdown means *something* cut
power. Whether it was a lockup is answered only by comparing the end of that
boot's journal with the survey's last write:

```bash
journalctl -b -1 --no-pager | tail -3              # when did logging stop?
journalctl -b -1 -u rfsurvey@uhf | grep stats | tail -1   # when did the survey?
```

Same second: power pull on a healthy machine. Hours apart: the fault.

---

## A power pull also corrupts the clock

The Pi 5's RTC has no battery fitted. It keeps time across a **warm** reboot and
loses it on a **power pull**, after which the clock is restored to a stale date
and the survey starts before chrony corrects it. Event times are anchored at
window open, so the whole run is misdated for its lifetime:

```
boot ended  how         chrony                   run   dated
06:11       warm        no step                  16    correctly
06:43       power pull  wrong by 4356004 s       17    2026-07-27
08:15       power pull  wrong by 4386429 s       18    2026-07-27
15:32       warm        no step                  19    correctly
```

Four for four. Runs 5, 17 and 18 are misdated this way, and all three are
exactly recoverable: chrony logged the offset to the microsecond.

**At Black Rock there is no NTP at all**, so chrony never corrects anything and
every run after a power interruption is misdated permanently. Software cannot
recover an absolute time that nothing on the machine knows. That needs the
RTC battery, or a GPS time source.

**Meanwhile, reboot with `sudo reboot`, never by pulling power** — a pull both
misdates the next run and resets the journald experiment.

---

## The second fault: WiFi steering, not the drive (2026-09-16)

The two lockups on 2026-09-16 at 06:43 and 08:15 happened **with APST already
disabled**, and they were a different fault. The disk, journal and survey were
fine throughout; what disappeared was the network.

The router (SSID `MobiusStripClubSandwich`, BSSIDs `42:75:c3:fe:b7:f9` and
`42:75:c3:05:b7:fa`) sends 802.11v BSS Transition Management requests every
15–40 minutes, trying to move the Pi between its two radios. `brcmfmac` cannot
handle the frame, and the Pi drops its link every time:

```
wpa_supplicant  wlan0: WNM: Preferred List Available
kernel          brcmf_p2p_send_action_frame: Unknown Frame: category 0xa, action 0x8
networkd        wlan0: Lost carrier / DHCP lease lost
wpa_supplicant  CTRL-EVENT-DISCONNECTED ... reason=3 locally_generated=1
```

Sixteen drops across four boots that day. Most recover in ~17 s, some in ~1m45s.
**The 06:43 one never recovered:** steered at 06:38:57, authentication to
`05:b7:fa` timed out, the original AP then refused it back
(`ASSOC-REJECT status_code=16`), it associated to `05:b7:fa` at 06:39:22 and
**never got a DHCP lease** — link up, no address, until power was pulled four
minutes later.

**The survey does not care.** Straight through the 17:18–17:20 drops it held
76.3 fps and zero overflows; it needs no network. A "lockup" of this kind is a
loss of remote access, not a loss of data — whereas pulling power to clear it
costs a run boundary, an unsafe shutdown, and (with no RTC battery) a misdated
run.

### Telling the two faults apart

| | NVMe APST (fixed) | WiFi steering (open) |
|---|---|---|
| ping | answers | no response |
| SSH | connects, stalls before banner | cannot connect |
| journal | goes silent, often hours early | keeps writing to the end |
| survey | keeps running | keeps running |
| in the journal | nothing — it cannot write | `WNM` / `Lost carrier` |

### What the steering actually is: 2.4 GHz vs a weak 5 GHz

Identified the same evening from `wpa_cli scan_results`. The two BSSIDs are the
two radios of one Xfinity gateway:

| BSSID | band | signal at the deck |
|---|---|---|
| `42:75:c3:fe:b7:f9` | 2.4 GHz, ch 1 | **−48 dBm** |
| `42:75:c3:05:b7:fa` | 5 GHz, ch 157 | **−69 dBm** |

This is band steering. The gateway pushes the Pi toward 5 GHz, where the signal
at the deck is 21 dB weaker and **DHCP does not complete**, and while enforcing
the move it rejects association on 2.4 GHz (`ASSOC-REJECT status_code=16`). Both
stuck outages — 06:39 on 2026-09-16, and a deliberate supplicant restart at 17:59
— were this exact sequence: rejected on 2.4 GHz, associated on 5 GHz, no IPv4
lease. Every recovery that day ended on 2.4 GHz.

### `disable_btm=1` made it worse, and was reverted

Applied 2026-09-16 ~18:00 and removed at 20:36, on evidence. It did exactly what
it is designed to do — stop advertising BSS Transition support — and the gateway
answered by kicking the Pi off instead of asking it to move:

| | 2.4 h before | 2.6 h after |
|---|---|---|
| 802.11v steering requests | 7 | **0** |
| gateway deauthenticated the Pi (reason 7) | 0 | **18** |
| association refused on 5 GHz | 0 | 9 |

A steering request cost ~17 s of outage. A kick cost 2–10 minutes, and the deck
was unreachable for most of that evening. **Some band-steering gateways fall back
to hard steering for clients that refuse soft steering; this one does.** Do not
reapply it on this network.

### 2.4 GHz only — applied 2026-09-16 20:41

```yaml
      access-points:
        "MobiusStripClubSandwich":
          band: 2.4GHz          # <- added
          auth: ...
```

Netplan turns this into `freq_list=` (the 2.4 GHz channels) in the network block,
so the Pi can never associate to the 5 GHz radio where DHCP fails. It is scoped to
this one SSID and constrains no other network the deck joins.

**It must go inside the existing access-point entry.** Putting it in a separate
overlay file (`90-*.yaml`) makes netplan *replace* the access point rather than
merge into it: tested offline with `netplan generate --root-dir`, the result lost
the password and came out `key_mgmt=NONE`. Applied live, that would have taken
the deck off the network entirely.

Applied by a detached job with an eight-minute automatic rollback; back on
2.4 GHz with an address in 10 s. Backup of the original file:
`/var/backups/netplan-50-cloud-init.yaml.pre-2g4`. To undo by hand:

```bash
sudo install -m 600 /var/backups/netplan-50-cloud-init.yaml.pre-2g4 /etc/netplan/50-cloud-init.yaml
sudo netplan generate && sudo systemctl restart netplan-wpa-wlan0.service
```

**Open question, to be answered by the watchdog's log:** `freq_list` restricts
which radio the Pi joins, not necessarily which bands it scans. If it still
probes on 5 GHz the gateway may keep classifying it as dual-band and keep sending
steering requests. Whether drops continue will show in
`journalctl -u rfsurvey-netwatch` as `network lost` / `network restored` pairs.

### The watchdog, redesigned for a deck with no network

`rfsurvey-netwatch.timer`, once a minute. The first version acted on any failed
gateway ping — wrong for a field deck, where no network is the normal state, and
no help at home, where its restarts only reset the gateway's steering timers.

It now acts in exactly one state: **associated and authenticated
(`wpa_state=COMPLETED`) but no IPv4 address.** An access point accepted the Pi
and DHCP never finished; nothing outside explains that. Escalation: renew DHCP at
2 min, reassociate at 5, restart the supplicant at 10, then every 15 and 30.

Everything else — no network in range, rejected or kicked, gateway not answering
ping — gets no action. Outages are logged as `network lost` / `restored` only
after the network has been up once this boot, so **a deck that never sees a
network logs nothing at all.** Simulated before install:

| scenario | log lines | actions |
|---|---|---|
| field, no network for 3 h | 0 | 0 |
| gateway kicks it off for 6 min | 2 | 0 |
| associated, no address, 30 min | 6 | renew, reassociate, restart, restart |
| up, gateway not answering ping, 20 min | 2 | 0 |
| 60 min no network, then a phone hotspot | 2 | 0 |

### Other options, if drops continue

- **Ethernet.** `eth0` is already configured for DHCP (`optional: true`) and
  unplugged. Removes the fault at home with no configuration at all.
- **Turn off band steering / 802.11v for this device on the router.** Fixes it at
  the source; does not travel with the deck.
- ~~Restrict this SSID to 2.4 GHz~~ — applied, above.
- ~~`disable_btm=1`~~ — applied and reverted, above.

**If the deck is unreachable, wait before pulling power.** The survey keeps
running regardless, and a pull costs a run boundary, an unsafe shutdown and —
without an RTC battery — a misdated run.

---

## Timestamps: clock repair applied 2026-09-16

Six runs recorded under a pre-NTP clock were corrected in `data/phase4.sqlite`
(backup: `data/phase4.pre-clock-repair.sqlite`), using chrony's logged step for
each boot:

| run | events | offset applied (s) |
|---|---|---|
| 5 | 14,956 | 3,026,478.577405 |
| 6 | 116,011 | 3,053,108.017635 |
| 8 | 662 | 4,329,195.433300 |
| 9 | 656 | 4,349,469.052112 |
| 17 | 17,710 | 4,356,004.075303 |
| 18 | 3,906 | 4,386,429.379779 |

Chrony's figures were checked against the data before being used. Each run's
journal `ended` lines (correct wall time after the step) were matched to its
database `t_end` values, using only unambiguous matches and restricted to the
run's own PID. Every repaired run's residual fell inside the range shown by
correctly dated control runs, so the step accounts for the whole clock error.
Values stamped after the step (`runs.ended_at`, a cleanly closed window's
`t_end`) were left alone, and they now agree with the shifted event times to
within 1–2 s.

**A separate bug the calibration exposed, not yet fixed:** every event in every
run, correctly dated or not, is stamped about **1.5–2 s early**. The control runs
show journal-minus-`t_end` of +1.83 to +2.34 s, of which ~0.3 s is the detector's
hang time. The likely cause is `window_t0` being taken at window open, before the
linearity check reads samples that the event clock never counts. Unconfirmed.

---

## Field WiFi networks — added 2026-09-16

`/etc/netplan/50-cloud-init.yaml` now holds three access points: the home network
(2.4 GHz only), **`jnwwifi`** (the WildFire lodge) and **`What iPhone?`** (the
operator's hotspot). Added inside the existing `access-points:` map for the same
reason as the band setting — an overlay file replaces the map.

Verified before applying: netplan generated three WPA-PSK networks, the home one
kept its `freq_list`, and both new passphrases matched byte-for-byte what was
supplied, checked programmatically rather than by eye. The hotspot password
contains an apostrophe; iOS Smart Punctuation can store a curly `’` in its place,
which derives a different key — if the Pi will not join the hotspot, that is the
first suspect. Applied with automatic rollback; home network back in 5 s with all
three loaded. Backup: `/var/backups/netplan-50-cloud-init.yaml.pre-fieldwifi`.
