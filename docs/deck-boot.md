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

**`unsafe_shutdowns` should now move only on a deliberate power pull.** It read
8 on 2026-08-31 and 11 on 2026-09-16 — three lockups in that fortnight, which is
the fault running untreated while believed fixed. If it climbs again now that
APST is off, APST was not the whole story.
