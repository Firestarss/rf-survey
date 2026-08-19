# Handoff — state of the software as of 2026-08-19

Written for whoever picks this up next, agent or human. Covers what exists, why it is
shaped this way, and what is deliberately unfinished. Read this before changing the
schema or the enricher; several things that look arbitrary are load-bearing.

Hardware has not arrived. Everything below was built and tested against synthetic
events. **Nothing here has ever seen a real signal.** Treat every threshold as a guess
until Phase 1 through 3 say otherwise.

---

## 1. What is in the repository

```
src/survey_prototype.py   detector. 912 lines. Predates everything else here.
src/db.py                 connection, schema init, run lifecycle, log_event
src/schema.sql            fresh-database shape. Stamps v2; migrations carry it forward.
src/migrate.py            incremental migrations. Never re-paste schema.sql again.
src/bandplan.py           frequency -> label lookup
src/enrich.py             tag, rollup, pair, score
tools/seed_band_plan.py   FRS/GMRS/MURS/Part 90 channels + ARRL ham segments
tools/make_fixtures.py    deterministic synthetic festival scenario
tools/deck-check.sh       soak and diagnostics
profiles/festival.yaml    receiver assignments, detection thresholds, operator licences
docs/phase_log.md         gate tracker. Phase 0 PASS.
```

Run the whole chain with no hardware:

```bash
python3 src/db.py data/survey.sqlite
python3 tools/seed_band_plan.py data/survey.sqlite
python3 tools/make_fixtures.py data/survey.sqlite --wipe
python3 src/enrich.py data/survey.sqlite --profile profiles/festival.yaml
python3 src/survey_prototype.py --selftest
```

---

## 2. Decisions that are load-bearing

**Receivers are addressed by serial, never by index.** USB enumeration order changes
across reboots. Addressing by index will one day swap the two bands silently and every
row logged after that will be wrong.

**Frequencies are INTEGER Hz, times are REAL unix epoch seconds.** Integer Hz because
466.0 MHz must round-trip exactly. Epoch floats because repeater pairing correlates
keyups tens of milliseconds apart, and ISO strings make that comparison expensive.

**`band_plan` stores ranges, not frequencies.** A discrete channel is a narrow range; a
ham band segment is a wide one. One containment query serves both, narrowest match
wins, so 146.520 resolves to the national calling channel while 146.470 falls through
to the enclosing simplex segment. The match tolerance lives in the data rather than as
a constant inside the enricher.

**Channel match windows are computed, not written by hand.** Each is the smaller of half
the authorised bandwidth and half the gap to the nearest neighbour in the same service.
FRS primary and interstitial channels interleave to 12.5 kHz spacing, and 151.505 sits
7.5 kHz from 151.5125 — hand-written tolerances would have overlapped silently.
`seed_band_plan.py` checks for overlaps on every run and prints the count. It must stay 0.

**`events` is append-only observation; `channels` and `pairs` are derived.** Delete the
derived tables and re-run `enrich.py` and they come back — except `channels.notes`,
which is hand-written and explicitly preserved across rebuilds.

**Repeater pairing requires correlated timing, not just offset.** 462.700 and 467.700
are 5 MHz apart whether or not a repeater links them. The fixtures contain a decoy at
exactly that offset with uncorrelated keyups; it must not be reported as a pair.

**FRS vs GMRS is settled by bandwidth or deviation, never by received power.** Received
power is transmit power minus path loss, and path loss varies by tens of dB across a
site, so a close FRS handheld reads louder than a distant repeater. The fixtures contain
a control for this: 462.650 is the loudest channel in the set at 56 dB SNR and must stay
labelled `FRS 19 / GMRS 19` because it is narrowband. The inference is one-sided — wide
rules FRS out, narrow rules nothing out, because narrowband GMRS radios are common.

**Tone agreement threshold is 0.8, not 0.6.** At 0.6 a shared FRS channel where a third
of traffic runs CTCSS was recorded as "no tone" and promoted to tier 4, which would have
you programme a radio the tone-squelched third never hears.

**`tone_state` distinguishes "confirmed clean" from "never checked".** Both were NULL
before migration 3. They are very different facts and the tier ladder turns on it.

**`runs.profile_yaml` holds the whole profile verbatim, not a path.** The file on disk
drifts; the run must keep saying what it actually used.

---

## 3. The unfinished join: prototype to database

`survey_prototype.py` was written before `db.py` and has its own `open_db()` creating a
rival `events` table. The two have never been connected. This is the next task.

**The collision.** `open_db()` uses `CREATE TABLE IF NOT EXISTS`, so against an existing
`data/survey.sqlite` it is a silent no-op and every subsequent INSERT fails on missing
columns. Loud rather than corrupting, but it must be removed.

### Field mapping

| Prototype | Schema | Note |
|---|---|---|
| `ts_start` | `t_start` | |
| `ts_end` | `t_end` | |
| `peak_snr_db` | `snr_db` | |
| `freq_hz` | `freq_hz` | already snapped to a 6250 Hz grid |
| `freq_error_hz` | — | residual within channel; consider `freq_raw_hz` |
| `deviation_hz` | — | **no column yet.** Needed for the FRS/GMRS rule |
| `ctcss_hz` | `ctcss_hz` | |
| `ctcss_conf` | `confidence` | energy capture ratio, 0–1 |
| `dcs_suspected` | `tone_state='dcs'` | **no codeword** — see below |
| `overload` | — | no column yet; worth adding |
| `coverage` table | `run_receivers` | roughly equivalent |

### Direction of change, decided

- **Prototype conforms** on naming, table structure, and `--receiver-id` (defaults to
  `rx0`; must be `uhf`/`vhf` to match the profile and schema).
- **Schema conforms** on the two-phase write. The prototype inserts on keyup and updates
  on release, which is correct for an unattended deck that can lose power mid-
  transmission — those in-flight rows are the ones worth keeping. `events.t_end` and
  `events.duration_s` are currently `NOT NULL` and must become nullable. That is
  migration 5, along with adding `deviation_hz` and `overload`.
- **Enricher conforms** on frequency binning. `FREQ_BIN_HZ` is 2500, finer than the
  prototype's 6250 Hz grid and therefore meaningless. Set it to 6250.
- **Discriminator switches from bandwidth to deviation.** `enrich._narrow_by_bandwidth`
  reads `events.bandwidth_hz`, which the prototype never populates. Deviation is already
  measured and has a tighter regulatory limit: FRS peak deviation is capped at 2.5 kHz,
  GMRS wideband runs about ±5 kHz. Rewrite against `deviation_hz` and update the fixtures
  to match, keeping the loud-but-narrow control case.

### Capability gaps that conforming cannot fix

- **`content` is never determined.** Nothing classifies voice versus data. The tier
  ladder in `enrich.score` gates tier 2 on `content`, so against real data every channel
  caps at tier 1. Ladder rework below.
- **DCS is suspected, never decoded.** `dcs_suspected` is a boolean with no codeword;
  "probably some DCS" will not programme a radio. Decoding the 23-bit Golay word at
  134.4 bps is well-defined work and would move channels from tier 2 to tier 3.
- **Transmissions shorter than `ANALYZE_SECONDS` (1.4 s) are never analysed at all.**
  Most festival traffic is shorter than that. Those events get no tone, no deviation,
  nothing. Consider analysing whatever dwell exists and reporting lower confidence.

### Agreed tier ladder, not yet implemented

| Tier | Means | Gate |
|---|---|---|
| 0 | Heard it | too short to analyse — frequency and time only |
| 1 | Analysed | deviation and frequency error measured; narrow vs wide known |
| 2 | Tone resolved | CTCSS value known, or confirmed clean. DCS-suspected stops here |
| 3 | Programmable | everything a radio needs, including the input if it is a repeater |
| 4 | Joinable | and the operator may legally transmit |

Drops `content` from the ladder entirely. Each rung is something the prototype can
actually establish today.

---

## 4. Open questions

**One tone per channel, or a distribution?** A repeater has one CTCSS. A shared FRS
channel at a festival has three, belonging to three different groups. The schema has a
single tone field, so contested channels currently collapse to `unknown` — honest, but
it discards the fact that there are three distinct populations.

**The 451.800/456.800 itinerant repeater pair falls outside every scan window.** Reaching
it needs a fourth rotation window near 454 MHz, which costs dwell time on the three that
exist. Deferred until a real deployment shows whether that band is busy.

**Ham segments are ARRL national, not NESMC.** Correct for the country, wrong in detail
for Massachusetts. Every row carries a `source` column; re-seeding replaces them by
`(service, label, freq_lo_hz)`.

**Deviation and bandwidth measurement accuracy is unverified.** The FRS/GMRS rule depends
on it. Phase 3 should transmit a known narrowband signal and a known wideband one and
compare against what the deck reports. If it is sloppy, raise the evidence margin until
false positives stop.

---

## 5. Not recoverable from the repository

`rm -rf *` in the home directory on 2026-08-19 destroyed five design documents that were
never restored: `design-decisions.md`, `bench-bringup.md`, `rf-primer.md`,
`pi-architecture.md`, `phase1-detail.md`. The bench bring-up plan in particular is the
nine-phase gated procedure to follow the day the Airspys arrive — it exists only in the
original design conversations. Restore it before hardware lands.

`docs/band-plan-notes.md` was written but never installed, and its coverage section
describes the old 153.200 MHz VHF window rather than the current 154.950.

---

## 6. Housekeeping still outstanding

- README points at `tools/phase-log.md`; the file is `docs/phase_log.md`
- DHCP reservation for `radio-deck` — it moved .243 to .244 mid-session once already
- WiFi power save disabled via systemd oneshot (no NetworkManager on Ubuntu Server)
- Map physical USB ports to buses and label the case. The two Airspys must land on
  different 480M root hubs. Doable today with a thumb drive; retires a Phase 6 gate
- GitHub access is a full account key on a machine going into a backpack. A repo-scoped
  deploy key has the same convenience and a blast radius of one repo
- The repository is public. `data/` is gitignored so the database will not leak, but
  `docs/` will accumulate site notes and observed frequencies from real deployments
