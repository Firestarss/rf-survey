# Pi architecture and filesystem

Where things live on `radio-deck`, and how the pieces fit together. Written for the Ubuntu
26.04 install now booting from NVMe.

Two layouts: **development**, which is where you are, and **deployed**, which is where this
ends up. Don't build the deployed layout yet — it's here so the development one grows in the
right direction rather than needing to be untangled later.

---

## 1. Development layout — set this up now

A git repo in your home directory. Everything runs from here, as your own user, with no
system integration at all.

```
~/rfsurvey/                     git repo, the only thing that matters
├── .git/
├── .gitignore
├── README.md
├── src/
│   └── survey_prototype.py     the detector
├── tools/
│   ├── deck-check.sh           soak and diagnostics
│   └── phase-log.md            your running gate results
├── docs/
│   ├── design-decisions.md
│   ├── bench-bringup.md
│   ├── rf-primer.md
│   └── pi-architecture.md      this file
├── profiles/
│   └── festival.yaml           receiver assignments (see §5)
└── data/                       gitignored — everything below is disposable
    ├── bench.sqlite
    ├── spectrum/               PNGs from --spectrum
    └── soak/                   CSVs from deck-check
```

`.gitignore`:

```
data/
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.png
*.csv
__pycache__/
```

**Why a repo now.** You're about to start changing the detector — thresholds, band plans,
the pairing logic — and you'll want to know which version produced which result. When you
send me output from a failed gate, "commit a3f21c" is a far better answer than "the current
one." It also makes the eventual move to `/opt` a checkout rather than a copy.

### The one piece of system setup worth doing today

Without this, talking to the Airspy needs `sudo`, and running an SDR as root is both
unnecessary and annoying.

```bash
sudo tee /etc/udev/rules.d/52-airspy.rules <<'EOF'
# Airspy R2 / Mini
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="60a1", MODE="0660", GROUP="plugdev"
EOF

sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules
# log out and back in for the group to take effect
```

Do this before the radio arrives so first plug-in just works. Verify afterwards with
`id` — you should see `plugdev` in your groups.

---

## 2. Deployed layout — later, after Gate 8

Standard FHS. Nothing exotic, and it'll look familiar.

```
/opt/rfsurvey/                  code, deployed as a git checkout
├── src/
├── tools/
└── venv/                       if you end up needing one

/etc/rfsurvey/                  configuration, hand-edited
├── deck.yaml                   receiver serials, gain, ppm, paths
└── profiles/
    ├── festival.yaml
    └── survey.yaml

/var/lib/rfsurvey/              data the service owns
├── survey.sqlite
├── audio/YYYY-MM-DD/           per-event audio, rotated
├── iq/YYYY-MM-DD/              IQ snippets for unclassified events, rotated first
└── spectrum/                   periodic band captures

/var/log/rfsurvey/              logs, or just use journald
```

**Why split code, config and data.** Code is replaceable from git. Config is the thing you
hand-edit and want to back up. Data is large and disposable in a specific order — IQ first,
then audio, never metadata. Keeping them in separate trees means rotation and backup
policies don't have to be clever.

### Service user

```bash
sudo useradd --system --home /var/lib/rfsurvey --shell /usr/sbin/nologin rfsurvey
sudo usermod -aG plugdev rfsurvey
sudo install -d -o rfsurvey -g rfsurvey /var/lib/rfsurvey /var/log/rfsurvey
```

The service shouldn't run as you, and definitely shouldn't run as root. It needs USB access
(`plugdev`) and write access to its own data directory. Nothing else.

---

## 3. Process architecture

One process per receiver, sharing one database. Not one process handling both.

```
        radio-deck
        ┌──────────────────────────────────────────────┐
        │                                              │
  USB   │  rfsurvey@uhf.service                        │
 bus002 ├─►  Airspy #1 → detect → analyse → ──┐        │
        │    parked 466.0 MHz                 │        │
        │                                     ▼        │
        │                            /var/lib/rfsurvey │
        │                              survey.sqlite   │
        │                                (WAL mode)    │
  USB   │                                     ▲        │
 bus004 ├─►  Airspy #2 → detect → analyse → ──┘        │
        │    rotating 446.0 / 146.0 / 153.2            │
        │  rfsurvey@vhf.service                        │
        └──────────────────────────────────────────────┘
```

**Why separate processes.** A crash in one radio's chain doesn't take the other down, each
gets its own systemd restart policy, and you can stop one to work on it without losing
coverage on the other. It also maps cleanly onto the USB topology — one process, one bus, one
radio.

**Why one database.** The pairing engine needs to see both receivers' events to correlate a
repeater's two halves. SQLite in WAL mode handles two writers fine; it's already set in the
prototype.

Later additions slot in as further subscribers to the same database rather than changes to
these two:

| Process | Role | Added at |
|---|---|---|
| `rfsurvey@uhf` | receiver 1 | Phase 2 |
| `rfsurvey@vhf` | receiver 2 | Phase 6 |
| `rfsurvey-pair` | repeater matching across both | Phase 7 |
| `rfsurvey-rollup` | maintains the `channels` table from `events` | Phase 7 |
| `rfsurvey-web` | read-only query interface | after Gate 8 |

---

## 4. systemd, when you get there

A templated unit so both receivers share one file:

```ini
# /etc/systemd/system/rfsurvey@.service
[Unit]
Description=RF survey receiver %i
After=network.target

[Service]
Type=simple
User=rfsurvey
Group=rfsurvey
WorkingDirectory=/opt/rfsurvey
ExecStart=/usr/bin/python3 /opt/rfsurvey/src/survey_prototype.py \
    --config /etc/rfsurvey/deck.yaml --receiver-id %i
Restart=always
RestartSec=10
Nice=-5

# it only needs its own data directory
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/rfsurvey /var/log/rfsurvey

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now rfsurvey@uhf rfsurvey@vhf`.

`Nice=-5` because sample capture is latency-sensitive — a late read means a dropped buffer,
and a dropped buffer looks like a signal that was never there.

**The hardware watchdog is already enabled** (`dtparam=watchdog=on` is in your config.txt).
To use it:

```bash
sudo apt install -y watchdog
# /etc/watchdog.conf:
#   watchdog-device = /dev/watchdog
#   file = /var/lib/rfsurvey/heartbeat
#   change = 300
```

Have the service touch `heartbeat` each time it commits. If it stops for five minutes the Pi
reboots itself. That's the difference between losing an hour of a festival and losing the
rest of it.

---

## 5. Profiles as files

A profile is the answer to "where am I and what do I care about." It's config, not code.

```yaml
# /etc/rfsurvey/profiles/festival.yaml
name: festival
description: FRS, GMRS, Part 90 business, plus 2m and 70cm ham

receivers:
  uhf:
    serial: "0x1234ABCD"        # never address by index
    mode: parked
    center_hz: 466_000_000
    sample_rate: 10_000_000
    gain: 12
    ppm: 0.0
    attenuator_db: 20           # what's physically fitted, for the record
    antenna: "465 MHz half-wave"

  vhf:
    serial: "0x5678EF01"
    mode: rotating
    dwell_seconds: 180
    sample_rate: 10_000_000
    gain: 12
    windows:
      - {center_hz: 446_000_000, label: "70cm ham"}
      - {center_hz: 146_000_000, label: "2m ham"}
      - {center_hz: 153_200_000, label: "MURS + VHF business"}

detection:
  on_db: 10.0
  off_db: 6.0
  min_duration_s: 0.12
  hang_s: 0.30
```

Recording `attenuator_db` and `antenna` matters even though software can't read them — six
months on, "why is this run 10 dB down on that one" is answered by the config file rather
than by memory.

---

## 6. Storage

| What | Rate | Rotation |
|---|---|---|
| `survey.sqlite` metadata | ~50 MB/day | **never delete** |
| `audio/` Opus per event | ~500 MB/day | 30 days |
| `iq/` snippets, unclassified only | ~5 GB/day | 7 days, **delete first** |
| `spectrum/` PNGs | negligible | 90 days |

**A week of continuous logging is under 40 GB.** Your 465 GB is roughly three months. There
is no capacity problem here and no reason to be clever.

Rotation order matters and is deliberate: IQ is the largest and least valuable once a signal
has been classified. Metadata is small and irreplaceable. A simple `systemd-tmpfiles` rule or
a nightly `find -mtime +N -delete` covers it.

**SQLite housekeeping:** WAL mode is already set. Add a weekly `PRAGMA wal_checkpoint(TRUNCATE)`
and an occasional `VACUUM` when the service is stopped. Don't `VACUUM` while it's running.

---

## 7. Naming

Consistency here saves confusion later, particularly in log messages and database rows.

| Thing | Convention | Example |
|---|---|---|
| Receiver ID | short, lowercase, by band role | `uhf`, `vhf` |
| Profile | lowercase, by environment | `festival`, `survey` |
| Radio identity | **always serial, never index** | `0x1234ABCD` |
| Data files | ISO date directories | `audio/2026-08-30/` |
| Diagnostics | phase-tagged | `phase2-20260830-1412.txt` |

The serial point is worth repeating: USB enumeration order changes between reboots. Address
by index and one day the two radios silently swap bands, with no error and a log that looks
entirely plausible.

---

## 8. What to do now versus later

| Now, before the radio arrives | Later, after Gate 8 |
|---|---|
| Create the git repo, move the files in | `/opt`, `/etc`, `/var/lib` layout |
| Add the udev rule, join `plugdev` | Service user |
| Write `profiles/festival.yaml`, even partially | systemd units |
| Start `tools/phase-log.md` | Watchdog heartbeat |
| — | Rotation rules |
| — | Read-only query interface |

Everything in the right column depends on something in the left column already working.
None of it is hard; all of it is premature today.

---

## 9. Known specifics of this machine

Recorded so they're not rediscovered later:

- **USB:** one radio on a `Bus 002` port, one on `Bus 004`. Confirm with `lsusb -t` after
  plugging both — they must sit under different `480M` root hubs.
- **A third USB 2.0 controller exists** on the USB-C connector (`Bus 001`, `dwc2`). Currently
  used for power. Possible route to a third radio later, if the Pi is powered via GPIO
  instead.
- **PCIe is Gen2 x1** and deliberately left there. Roughly 450 MB/s against a workload
  needing single-digit MB/s.
- **`vcgencmd` is installed but `/dev/vcio` is absent**, so throttle registers don't work.
  Use CPU clock from `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq` instead — if it
  sits below 2400 MHz under load, that's throttling.
- **`eth0` is down, WiFi only.** Fine for SSH and for `--spectrum` captures. If you want live
  remote spectrum via SoapyRemote at full rate that's ~30 MB/s and wants ethernet; over WiFi,
  drop to 2.5 MSPS for browsing.
- **AppArmor logs denials against `lsusb`** on 26.04. Cosmetic — output is complete.
