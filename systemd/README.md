# Running the deck as a service

The survey is something you **start and stop**, not something that is always on.
The Pi has other uses, and a deck that comes back by itself after you deliberately
stopped it is worse than one that does not run at all. But while it *is* running it
is unattended, and nobody is going to notice a crashed process in a field — so it
runs under systemd with a restart policy rather than from a shell.

## Install

Installing makes the commands available. It does **not** start anything, and it
does not run anything at boot.

```bash
sudo cp systemd/rfsurvey@.service systemd/rfsurvey.target /etc/systemd/system/
sudo systemctl daemon-reload
```

Deliberately no `systemctl enable`. Enabling is what makes a unit start at boot,
and that is the behaviour we do not want. If you ever do want the deck up
automatically from power-on, `sudo systemctl enable rfsurvey.target` is the switch
— but then it is running every time the Pi boots, whatever else you had planned
for it.

## Start and stop

Both receivers together:

```bash
sudo systemctl start rfsurvey.target
sudo systemctl stop  rfsurvey.target
```

Or one at a time — the instance name becomes `--receiver-id`, so it must match a
key under `receivers:` in the profile:

```bash
sudo systemctl start rfsurvey@uhf
sudo systemctl stop  rfsurvey@vhf
```

Stopping sends SIGINT, not SIGTERM. The capture loop handles SIGINT: it closes the
in-flight events, closes the coverage window and ends the run, instead of being
killed mid-transaction and leaving the last window open. Give it a moment.

Watch it:

```bash
journalctl -u rfsurvey@uhf -f
systemctl status 'rfsurvey@*'
```

## What restarts and what does not

`Restart=on-failure`:

| what happened | result |
|---|---|
| `systemctl stop` | stays stopped |
| clean exit | stays stopped |
| crash, or the stream stops delivering | restarted after 10 s |
| ten failures in five minutes | gives up, stays stopped |

The stream case is the one worth recovering from. `survey_prototype` exits non-zero
after `STALL_FRAMES` consecutive empty reads, because a wedged USB endpoint does
not come back in-process — only re-enumeration fixes it, and that needs a fresh
start of the process.

If the start limit trips, something is actually wrong: the radio is unplugged, the
profile is bad, or the database is unwritable. `journalctl -u rfsurvey@uhf -n 50`
will say which, and `systemctl reset-failed rfsurvey@uhf` clears the latch once
you have fixed it.

## Before this will work

- **USB permissions.** The `firestarss` user must be able to open the Airspys
  without root. Airspy's udev rules normally land in
  `/etc/udev/rules.d/52-airspy.rules`; confirm with `SoapySDRUtil --find` as the
  service user, not as root, because root will succeed either way and tell you
  nothing.
- **The serials must be in the profile.** `receivers.uhf.serial` and
  `receivers.vhf.serial` are both `null` today. Until they are filled in, both
  instances address the radios by driver alone and whichever enumerates first
  answers — which is exactly the silent band swap that `docs/handoff.md` section 2
  says never to allow. Phase 1 fills these in.
- **Disk.** `--capture-dir` retains per-event audio under `data/captures`, capped
  by `--capture-mb` (2000 MB by default). Capture stops at the cap; detection and
  logging carry on.

## Trying it without a radio

The unit runs the real thing, so there is nothing to dry-run. To exercise the same
code path with no hardware, run it by hand:

```bash
python3 src/survey_prototype.py --simulate 14 --receiver-id uhf
```
