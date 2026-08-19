# rfsurvey

Unattended RF survey deck for `radio-deck` (Raspberry Pi 5, 8 GB, Ubuntu 26.04 LTS, NVMe boot).

Detects, classifies, and logs every transmission across FRS, GMRS, MURS, Part 90 UHF business,
2 m ham, and 70 cm ham. Output is a searchable SQLite database with enough per-signal detail
to program a radio and join the conversation.

**Two Airspy R2 receivers:**

| Receiver | Role | Coverage |
|---|---|---|
| `uhf` | parked at 466.0 MHz | FRS, GMRS, Part 90 — both repeater halves |
| `vhf` | rotating | 446.0 MHz (70 cm), 146.0 MHz (2 m), 153.2 MHz (MURS + VHF business) |

---

## Layout

```
src/         detector and pipeline
tools/       diagnostics, soak scripts, phase log
docs/        design decisions, bring-up plan, RF primer, architecture
profiles/    deployment configs (antenna + filter + attenuation + band assignments)
data/        gitignored, disposable
```

## Running

```bash
python3 src/survey_prototype.py --selftest     # no hardware needed
python3 src/survey_prototype.py --spectrum     # headless PNG + text peak list
tools/deck-check.sh                            # soak and diagnostics
```

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
