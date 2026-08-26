# rfsurvey

Unattended RF survey deck for `radio-deck` (Raspberry Pi 5, 8 GB, Ubuntu 26.04 LTS, NVMe boot).

Detects, classifies, and logs every transmission across FRS, GMRS, MURS, Part 90 UHF business,
2 m ham, and 70 cm ham. Output is a searchable SQLite database with enough per-signal detail
to program a radio and join the conversation.

**Two Airspy R2 receivers:**

| Receiver | Role | Coverage |
|---|---|---|
| `uhf` | rotating, 300 s / 60 s | 466.0 MHz (FRS, GMRS, Part 90 — both repeater halves), 446.0 MHz (70 cm ham) |
| `vhf` | rotating, 180 s each | 146.0 MHz (2 m ham), 154.95 MHz (MURS + VHF business) |

The receivers are grouped by **how much attenuation each band needs**, not by service.
Phase 1 measured 446 and 466 wanting 4–5 dB and 146 and 155 wanting 17–20 dB; one radio
carries one pad, so a receiver spanning both would be wrong for at least one of its
windows. See `docs/phase_log.md`, Phase 1.

---

## Layout

```
src/         detector and pipeline
tests/       the test suite (stdlib unittest, no dependencies)
tools/       diagnostics, soak scripts, phase log
systemd/     unit file and notes for running unattended
docs/        design decisions, bring-up plan, RF primer, architecture
profiles/    deployment configs (antenna + filter + attenuation + band assignments)
data/        gitignored, disposable
```

## Running

```bash
python3 src/survey_prototype.py --selftest              # sizing and speed, no hardware
bash tools/run-tests.sh                                # correctness: 222 tests
bash tools/deck-check.sh diag                          # diagnostics

# Look at a band without a monitor. Needs a radio; writes a PNG and prints the
# strongest channels, each with a measured frequency and the receiver's clock
# error against it.
python3 src/survey_prototype.py --spectrum band.png \
        --serial <SERIAL> --freq 466.0e6 --gain 42 --ppm 0.64
```

The capture loop runs without a radio too, against a synthetic one
(`src/simradio.py`). This drives detection, analysis, the two-phase write and the
retune logic end to end:

```bash
python3 src/survey_prototype.py --simulate 14 --receiver-id uhf
python3 src/survey_prototype.py --simulate 8 --rate 2.4e6 --receiver-id vhf \
        --dwell-seconds 6              # rotation across both windows
```

## Tests

```bash
bash tools/run-tests.sh                # everything, a couple of minutes on a Pi
bash tools/run-tests.sh test_enrich    # one module
RFSURVEY_SKIP_SLOW=1 bash tools/run-tests.sh   # skip the end-to-end capture run
```

Stdlib `unittest`, no third-party dependencies, because it has to run on the deck.
The end-to-end module drives the real capture loop against the synthetic radio and
asserts on the database that comes out; it is the only test that catches the class
of bug where every part works and the assembly does not.

## Current state

Phase 0 **PASS** (2026-08-18). Phase 1 blocked on Airspy delivery.
See `docs/phase_log.md` for the gate tracker.

## Conventions

- Receivers are addressed **by serial, never by index.** USB enumeration order changes
  across reboots; addressing by index will one day swap the two bands silently.
- Receiver IDs are short and lowercase by band role: `uhf`, `vhf`.
- Profiles are lowercase by environment: `festival`, `survey`.
- Data files go in ISO date directories: `audio/2026-08-30/`.
- Diagnostics are phase-tagged: `phase2-20260830-1412.txt`.
