# Running the deck as a service

The deck is unattended. Nobody is going to notice a crashed process in a field, so
it runs under systemd with a restart policy rather than from a shell.

One instance per receiver. The instance name becomes `--receiver-id`, so it must
match a key under `receivers:` in the profile:

```bash
sudo cp systemd/rfsurvey@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rfsurvey@uhf rfsurvey@vhf
```

Watch it:

```bash
journalctl -u rfsurvey@uhf -f
systemctl status 'rfsurvey@*'
```

Stop it cleanly before pulling the card — the unit sends SIGINT, which closes the
in-flight events and the coverage window instead of abandoning them:

```bash
sudo systemctl stop 'rfsurvey@*'
```

## Before this will work

- **USB permissions.** The `firestarss` user must be able to open the Airspys
  without root. Airspy's udev rules normally land in
  `/etc/udev/rules.d/52-airspy.rules`; confirm with `SoapySDRUtil --find` as the
  service user, not as root, because root will succeed either way and tell you
  nothing.
- **The serials must be in the profile.** `receivers.uhf.serial` and
  `receivers.vhf.serial` are both `null` today. Until they are filled in, both
  instances address the radios by driver alone and whichever enumerates first
  answers — which is exactly the silent band swap that
  `docs/handoff.md` section 2 says never to allow. Phase 1 fills these in.
- **Disk.** `--capture-dir` retains per-event audio under `data/captures`. The
  budget defaults to 2000 MB and capture stops at the cap; detection and logging
  carry on. Check free space before a multi-day deployment.

## Why Restart=always rather than on-failure

Under a supervisor there is no such thing as the survey finishing. A clean exit
is as unexpected as a crash and wants the same response. The one failure the code
handles explicitly is the stream going quiet — `survey_prototype` exits non-zero
after `STALL_FRAMES` consecutive empty reads, because a wedged USB endpoint does
not recover in-process and only re-enumeration fixes it.

`StartLimitBurst=10` in 300 s stops a genuinely broken deck from restarting
forever. If it trips, the radio is unplugged, the profile is wrong, or the
database is unwritable — `journalctl -u rfsurvey@uhf -n 50` will say which.
