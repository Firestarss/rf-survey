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
src/survey_prototype.py   detector. Writes through db.py; owns no schema of its own.
src/dcs.py                Golay(23,12) and DCS codewords. Read its docstring first.
src/simradio.py           synthetic Airspy, so the capture loop runs with no radio
src/db.py                 connection, schema init, run lifecycle, log_event
src/schema.sql            fresh-database shape. Stamps v2; migrations carry it forward.
src/migrate.py            incremental migrations. Never re-paste schema.sql again.
src/bandplan.py           frequency -> label lookup
src/enrich.py             tag, rollup, pair, score
tools/seed_band_plan.py   FRS/GMRS/MURS/Part 90 channels + ARRL ham segments
tools/make_fixtures.py    deterministic synthetic festival scenario
tools/deck-check.sh       soak and diagnostics
profiles/festival.yaml    receiver assignments, detection thresholds, operator licences
systemd/                  unit file and deployment notes for unattended running
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

And the capture loop itself, still with no hardware — this drives the detector, the
analyser, the two-phase write and the retune logic end to end:

```bash
python3 src/survey_prototype.py --simulate 14 --receiver-id uhf
python3 src/survey_prototype.py --simulate 8 --rate 2.4e6 --receiver-id vhf \
        --dwell-seconds 6                       # rotation across three windows
python3 src/dcs.py --check                      # is the DCS codeword table real yet
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

**FRS vs GMRS is settled by peak deviation, never by received power.** Received power is
transmit power minus path loss, and path loss varies by tens of dB across a site, so a
close FRS handheld reads louder than a distant repeater. The fixtures contain a control
for this: 462.650 is the loudest channel in the set at 59 dB SNR and must stay labelled
`FRS 19 / GMRS 19` because it is narrowband. The inference is one-sided — wide rules FRS
out, narrow rules nothing out, because narrowband GMRS radios are common.

**Only deviation measured above 18 dB SNR is evidence.** The estimator inflates on weak
signals and only ever toward "wide", which is the verdict that rules FRS out — so
ungated, the rule mislabels precisely the distant handheld it exists to protect. The
fixtures contain a control for this too: a distant FRS radio on 462.725 whose weak keyups
measure 5.4 kHz. Numbers and method in section 3.

**Tone agreement threshold is 0.8, not 0.6.** At 0.6 a shared FRS channel where a third
of traffic runs CTCSS was recorded as "no tone" and promoted to tier 4, which would have
you programme a radio the tone-squelched third never hears.

**`tone_state` distinguishes "confirmed clean" from "never checked".** Both were NULL
before migration 3. They are very different facts and the tier ladder turns on it.

**`runs.profile_yaml` holds the whole profile verbatim, not a path.** The file on disk
drifts; the run must keep saying what it actually used.

---

## 3. The join: prototype to database — done, 2026-08-19

`survey_prototype.py` had its own `open_db()` creating a rival `events` table, and the
two had never been connected. They are now. The prototype writes through `db.py` and
owns no schema of its own.

### What landed

**Migration 5 and an FK-safe migration runner.** `events.t_end` and `duration_s` are
nullable, so an in-flight row survives a power cut. Dropping a NOT NULL means a SQLite
table rebuild, which needs `foreign_keys = OFF` — and that pragma is a **no-op inside a
transaction**, which `apply()` wrapped every migration in. Without fixing that,
`DROP TABLE events` fires `ON DELETE CASCADE` and silently empties `decodes`. `apply()`
now sets the pragma outside the transaction, restores it, and runs `foreign_key_check`
before returning. The rebuild also added `deviation_hz`, `ctcss_dev_hz`, `overload`, the
`tone_state` CHECK that migration 3 could not add via ALTER, `channels.deviation_hz` and
`run_receivers.center_hz`.

`schema.sql` stays v2-shaped. It stamps v2 and `init_schema()` replays every migration on
top, so adding a column there too makes the matching ALTER fail with "duplicate column
name" on every fresh build. The two comments claiming otherwise are corrected. Fresh and
upgraded databases were diffed schema-for-schema and are identical.

**The prototype conforms.** `open_db()` and the private `coverage` table are gone.
`coverage` said nothing `runs` and `run_receivers` do not, once `center_hz` existed, and
nothing joined to it. The two-phase write lives in an `EventLog` class holding every
field mapping in one place, which is what lets `--selftest` drive the real code path
against a temporary database with no radio attached. `--receiver-id` is now
`choices=("uhf","vhf")` with no default, `--db` defaults to `data/survey.sqlite`, and
`--profile` is snapshotted verbatim into the run. The serial is read back off the device
rather than trusted from `--serial`.

**Field mapping, as built**

| Prototype | Schema | Note |
|---|---|---|
| `ts_start` | `t_start` | corrected for detector lag, below |
| `ts_end` | `t_end` | NULL while in flight |
| `peak_snr_db` | `snr_db` | |
| `freq_hz` | `freq_hz` | snapped to the 6250 Hz grid |
| `freq_error_hz` | `freq_raw_hz` | stored as `freq_hz + freq_error_hz`; the error recovers by subtraction, and this is the per-event ppm evidence Phase 1 checks against |
| `deviation_hz` | `deviation_hz` | now peak, not RMS — below |
| `ctcss_hz` | `ctcss_hz` | |
| `ctcss_conf` | `confidence` | clamped to 1.0 at source |
| `ctcss_dev_hz` | `ctcss_dev_hz` | |
| `dcs_suspected` | `tone_state='dcs'` | no codeword; caps the channel at tier 2 |
| `overload` | `overload` | |
| `coverage` table | `runs` + `run_receivers` | deleted |

**The discriminator is deviation, not bandwidth.** `_narrow_by_bandwidth` read
`events.bandwidth_hz`, which the detector has never populated and never will without new
code — so on real data the rule was NULL on every row and silently never fired. It is now
`_narrow_by_deviation`, against limits keyed by the authorised bandwidth the band plan
already records: 12.5 kHz channels cap at 2.5 kHz peak deviation, 20 and 25 kHz channels
at 5 kHz. The inference stays one-sided: wide rules FRS out, narrow rules nothing out.

**The tier ladder is implemented as agreed** — 0 heard, 1 analysed, 2 tone resolved,
3 programmable, 4 joinable. `content` is dropped from the ladder and left in the schema.

### Measured, and worth re-measuring against real signals

Two numbers were measured before deciding anything, and both changed the design. Neither
has been near a transmitter. **Phase 3 should re-measure both and correct them here.**

**1. Deviation was RMS, and a regulatory threshold cannot be compared to an RMS.**
`std(inst)` reads about 0.42x the true peak against synthetic voice, so a 2.5 kHz limit
from 47 CFR would never have fired at all. RMS also tracks how loudly someone is talking,
while peak deviation is pinned near the limit by the transmitter's own deviation limiter
and is stable across talkers. It is now p99 of `|inst|`:

```
                     true peak    std(inst)   p99|inst|
FRS-like  (2.5 kHz)      2700         1127        2537
GMRS wide (5 kHz)        5750         2581        5359
```

**2. The estimator inflates on weak signals, and only ever toward "wide".** FM clicks
below the demodulation threshold push the percentile up. The deck detects down to 10 dB:

```
in-chan SNR    narrow p99   wide p99
   51 dB          2537        5359
   17 dB          2864        5652
   13 dB          3185        5940
    9 dB          4068        6736   <- narrow now reads "wide"
```

"Wide" is the verdict that rules FRS out, so an ungated median mislabels exactly the
distant handheld the rule exists to protect. Hence `DEV_MIN_SNR_DB = 18.0`: only
measurements above 18 dB count as evidence, which keeps narrow <= 3200 and wide >= 5900
across the usable range, with a 1500 Hz margin (threshold 4000 Hz). `make_fixtures.py`
carries a regression case for this — a distant FRS handheld whose weak keyups measure
5.4 kHz and whose four close ones measure 2.4 kHz. Without the gate it is labelled
`GMRS 22`; with it, `FRS 22 / GMRS 22`.

**3. `ctcss_conf` overflowed `events.confidence`.** The capture ratio exceeds 1.0 on 6 of
the 54 standard tones (max 1.0009) because `tone_dev = 4.0 * mag` approximates the Hann
coherent gain. `confidence` is CHECK-constrained to [0,1], so unclamped, the cleanest
possible CTCSS signal — the most common thing at a festival — is the one that throws on
INSERT. Clamped at source in `analyze_analog`.

### Where this diverged from the plan above

Five changes were not in the original section 3 and are called out rather than buried:

- **Event timing had a systematic bias, now corrected.** `t_start` was recorded
  `min_duration` (0.12 s) after the signal actually started and the event closed `hang`
  (0.30 s) after it stopped, inflating every duration by ~0.42 s and every airtime total
  with it. `EventTracker` now keeps recent frame boundaries and reports both edges where
  they happened. Verified in `--selftest`: a 0.80 s signal logs as 0.80 s.

- **`FREQ_BIN_HZ` is 6250, but the bin is no longer what gets reported.** Binning at
  6250 groups measurements correctly, but its grid has arbitrary phase against real
  allocations — it filed the 146.820 repeater output as 146.8187, 1.25 kHz off, purely
  as an artefact. A discrete band plan channel reports its nominal frequency as before;
  anything else now reports the median of what was actually measured.

- **Suspected DCS survives the rollup.** It used to collapse to `tone_state='unknown'`
  whenever no codeword agreed — but with no decoder there is never a codeword, so the
  agreed "DCS-suspected stops at tier 2" rung was unreachable. Contested codewords still
  demote to unknown, exactly like a contested CTCSS value; no codeword at all now stays
  `dcs`, because "a subaudible signal that is demonstrably not CTCSS" is knowledge.

- **`enrich.pair()` crashed on an in-flight row.** It bounded its inner loop on
  `o["t_end"] + PAIR_MAX_LAG_S`, which is NULL for an event still in flight — the row
  the schema was just changed to allow. Now bounded on `t_start`, which is never NULL and
  is the tighter bound anyway, since the match test only accepts `|lag| <= PAIR_MAX_LAG_S`.

- **`v_contactable` was rebuilt in migration 5.** It rendered suspected DCS as `?`,
  which reads as "nothing known", and never showed the deviation the FRS/GMRS verdict
  rests on. It now shows `DCS?` and a `dev_hz` column.

### What conforming did not fix

- **`content` is never determined.** Nothing classifies voice versus data. This is why
  it left the ladder.
- **DCS is suspected, never decoded.** Decoding the 23-bit Golay word at 134.4 bps is
  well-defined work and would move channels from tier 2 to tier 3.
- **Transmissions shorter than `ANALYZE_SECONDS` (1.4 s) are never analysed.** Most
  festival traffic is shorter than that, and those events now correctly sit at tier 0
  rather than being scored on fields nothing filled in. Analysing whatever dwell exists
  and reporting lower confidence is the obvious improvement.

### Expected fixture movement, for whoever diffs against an older run

`463.1125` and `464.500` are now **tier 0** — every transmission on them is 0.15–1.0 s,
shorter than the analysis dwell, so a real deck would never analyse them. `462.5625`
(three groups, three tones, no agreement) drops 2 -> 1. `146.820` drops 3 -> 2: it is a
repeater output whose input is 600 kHz down and outside the parked window, so the deck
never hears it, and tier 3 requires an **observed** input rather than an assumed standard
offset. A repeater on a non-standard split would otherwise be programmed wrong, silently.

---

## 4. Second pass: the capture loop, run for the first time

`run()` had never executed. Everything else in the project had fixtures or a self
test; the capture loop needed a radio, so the detector, the two-phase write, the
retune logic and the database wiring were all unverified — together, at a festival.

`src/simradio.py` is a synthetic Airspy: it implements the handful of SoapySDR calls
`run()` makes and generates IQ containing transmissions whose answers are known.
`--simulate SECONDS` drives the whole path with no hardware.

```bash
python3 src/survey_prototype.py --simulate 14 --receiver-id uhf \
        --db data/survey.sqlite --capture-dir data/captures
python3 src/survey_prototype.py --simulate 8 --rate 2.4e6 --receiver-id vhf \
        --dwell-seconds 6            # exercises rotation across three windows
```

It is not a channel model — no path loss, no multipath, no adjacent-channel
splatter, and the "voice" is two sine tones. It cannot say whether the deck will
work at a festival. It says whether the code does what it claims, which is a
different and far cheaper question, and the answer was no.

### What running it found

Every one of these was invisible to inspection and to the existing self test.

**Deviation was measuring noise, on every event.** The analysis window starts
`PRETRIGGER_SECONDS` before the detector fires, so up to a third of it can be
carrier-free. An FM discriminator fed noise returns instantaneous frequencies
spread uniformly over +/- audio_fs/2, so p99 of that window reports ~11700 Hz no
matter what the transmitter was doing — and 11700 Hz is "wide", the verdict that
rules FRS out. Every measurement in section 3 was taken by calling
`analyze_analog` directly on a pure-signal window, which is why the estimator
looked accurate to within 4% while being useless in the deck. It now trims to
where the carrier actually is, using the constant envelope of an FM signal, and
`analyzed_s` records how much signal there turned out to be rather than how much
window was handed in.

**Long transmissions were truncated to 1.27 s.** `NoiseFloor` is a low percentile
over `FLOOR_FRAMES` of history. A carrier that stays up long enough to fill
(100 - `FLOOR_PCTILE`)% of that history drags the floor up to meet itself, the SNR
collapses and the detector declares the transmission over while it is still going.
At the defaults that is 1.26 s, and a 4.0 s transmission logged as 1.27 s — so a
30-second ham QSO would have logged as 1.27 seconds, and every airtime total with
it. Channels inside an active event now hold their previous floor instead of
contributing their own carrier to it.

**One transmission was logged as three.** An FM signal at 2.5-5 kHz deviation
occupies about 11 kHz and the detector grid is 6.25 kHz, so a single keyup lights
up its own channel and both neighbours. Because FRS primary and interstitial
channels interleave to 12.5 kHz, the two skirts land on real neighbouring channel
numbers: one GMRS keyup on 462.675 was reported as traffic on FRS 5 and FRS 6 as
well. Only a local maximum may now open an event.

**No transmission of any length ever had its tone identified.** The analysis
window was sized at `ANALYZE_SECONDS`, but it starts a pretrigger early, so after
the trim only 0.6 s of carrier survived — below `MIN_TONE_SECONDS`. The window is
now `ANALYZE_SECONDS + PRETRIGGER_SECONDS` long.

**`run_receivers.center_hz` was never written.** Migration 5 added the column and
`register_receiver()` silently drops kwargs not in its field list, so the centre
went missing with no error and the view reported 0.000 MHz.

### The profile is now obeyed

`run()` read every setting from the command line while snapshotting the profile
into the run row — so a run recorded a configuration it had not followed, which is
worse than recording none, because the snapshot reads as evidence. It now loads
`receivers.<id>` from the YAML for the centre, sample rate, gain, ppm, serial and
detection thresholds. Command-line flags remain, as overrides.

That also made rotation real. `receivers.vhf` has been configured `mode: rotating`
with three windows and `dwell_seconds: 180` since the profile was written, and
nothing implemented it: the receiver parked on whatever `--freq` said and two
thirds of its intended coverage was never listened to.

**Migration 7 adds `coverage_windows`**, one row per tune. This contradicts what
section 3 says about the prototype's old `coverage` table being redundant. That
was true for a parked receiver — `run_receivers` is `UNIQUE (run_id, receiver_id)`
and holds exactly one centre per receiver per run. It stops being true the moment
a receiver rotates, and "was anything on 2 m at 21:30, or were we parked on 70 cm"
is most of what a rotating receiver's log is worth. `v_coverage` reports it with
the honest denominator: a band with no events because nothing ever tuned to it
looks identical in `events` to a band that was quiet, and those are opposite
conclusions.

### What the test suite then found

`tests/` is stdlib unittest with no third-party dependencies, run with
`bash tools/run-tests.sh`. Writing it turned up four more faults, three of them in
code that had already been exercised by hand.

**`Ring.push` misplaced any block larger than the buffer.** The oversized-block
path wrote the surviving tail to `buf[0:]` instead of the slot its absolute index
maps to, so every later `get()` came back offset by `written % cap` — valid
samples from the wrong moment, which nothing downstream can detect. It needs a
frame larger than 1.9 s to trigger, so the deck never would; a lower ring size or
a larger frame would.

**Event timestamps mixed two clocks.** `t_start` and `t_end` were `time.time()`
minus an offset derived from the sample counter. Those agree only while samples
arrive in real time. They do not after an overflow delivers a burst, while the
process is descheduled, or under `--simulate` — and when they disagree by more
than the 0.18 s correction, an event is stamped as ending before it started, which
the schema rejects outright. Every timestamp now comes off the sample clock,
anchored once per window.

**Migration 8: `events.window_id`.** `v_coverage` matched events to windows by
timestamp range, reconstructing something the capture loop already knew. Under the
same clock divergence the ranges overlap, and events get attributed to a band that
was never tuned to — which is the one question coverage exists to answer. The
window is now recorded on the event.

**The simulator produced silence when noise was switched off.** Amplitude is
derived from the requested SNR relative to the noise in one channel, so with no
noise there was no signal — and the first phase-continuity test passed by
comparing silence to silence. A reference level now stands in, and that test
asserts the signal is non-trivial before comparing anything.

---

### Captures are retained

`events.audio_path` and `events.iq_path` have existed since v2 and nothing ever
wrote them. That is the one gap here that cannot be closed after the fact: a
festival happens once, every threshold in this repository is a guess, and without
recordings the deployment produces no material to correct those guesses against.

`--capture-dir` writes 8 kHz 16-bit audio per event (~16 kB per second of traffic).
`--capture-iq` additionally writes the complex channel the analyser saw, at ~24 kHz
(~190 kB/s), which is what lets a better tone or deviation algorithm be re-run
later against real signals. Both are capped by `--capture-mb`, checked before each
write and alongside free disk space: a deck that fills its disk mid-festival stops
logging events entirely, which is a far worse failure than losing recordings.

### Supervision

`systemd/rfsurvey@.service` runs one instance per receiver. `Restart=always`,
because under a supervisor a clean exit is as unexpected as a crash. `KillSignal`
is SIGINT so the loop closes its in-flight events and coverage window rather than
dying mid-transaction. The capture loop now counts consecutive empty reads and
exits non-zero after `STALL_FRAMES`, because a wedged USB endpoint does not recover
in process — only re-enumeration fixes it, and that needs a restart.
See `systemd/README.md`; the serials in the profile are still `null`, and until
Phase 1 fills them in both instances address the radios by driver alone.

---

## 5. Open questions

**One tone per channel, or a distribution?** A repeater has one CTCSS. A shared FRS
channel at a festival has three, belonging to three different groups. The schema has a
single tone field, so contested channels currently collapse to `unknown` — honest, but
it discards the fact that there are three distinct populations.

**The 451.800/456.800 itinerant repeater pair falls outside every scan window.** Reaching
it needs a fourth rotation window near 454 MHz, which costs dwell time on the three that
exist. Deferred until a real deployment shows whether that band is busy.

**`content` is still never determined, deliberately.** A voice/data classifier was
attempted and abandoned. The obvious model-free separator is the shape of the
demodulated distribution — 4FSK sits at discrete symbol levels, speech does not —
but measured on what this repository can generate:

```
signal                    dev p99   kurtosis
voice 2.4 kHz                2329       2.13
voice 4.9 kHz                4724       2.13
voice + CTCSS                2971       3.02
4FSK (DMR-like)              1985       1.76
4FSK noisy                   2387       1.83
```

Voice and 4FSK are 0.3 apart while adding a CTCSS tone moves voice by 0.9 — the
tone matters more than the modulation. Worse, the "voice" being measured is two
sine tones at 900 and 1700 Hz, which has nothing like the crest factor or the
pauses of speech, so any threshold picked here would be fitted to a fiction.

That is precisely the failure this project keeps hitting: the deviation estimator
was accurate to 4% against synthetic signals while reporting pure noise for every
event the deck actually logged. Shipping a second threshold with the same
provenance would add a column that looks authoritative and is not. The tier ladder
no longer depends on `content`, so nothing is blocked by leaving it NULL. Revisit
it with recorded audio from a real deployment — which `--capture-dir` now
produces — rather than with more synthetic signals.

**The DCS codeword table is not the real one, and 61 of 104 codes cannot be decoded
because of it.** `src/dcs.py` implements Golay(23,12) correctly — that part is
self-checking and tested, including error correction to the code's limit of three
bits. What is not established is the mapping from a three-digit code to the 23 bits
on the air. The code is *cyclic*, so all 23 rotations of a codeword are themselves
codewords, and the all-ones vector is a codeword so the complement of one is too. A
DCS transmission carries no sync pattern, just the word repeating forever, so
framing rests entirely on the fixed triple and the list of legal codes — and under
the convention implemented, transmitting 026 produces a bitstream that is also a
legal framing of 311. Those are the same periodic sequence; no receiver could
separate them. A real standard cannot have that property, so the real code set must
be chosen so that no two legal codes are rotations or complements of each other, and
the construction in `dcs.py` does not produce that set. Every plausible variation was
searched — fixed triple 0 through 7, LSB and MSB first, with and without polarity
search — and the best tops out at 70 of 104 framing uniquely.

The decoder therefore reports a code only where the framing is unambiguous and
reports "DCS present, code unknown" otherwise, which is what it did before decoding
existed. It never guesses: a wrong code sends you off to programme a radio that then
sits silent. **To finish this**, replace the codeword construction in `dcs.py` with
the real 23-bit word per code, from a verified reference or measured off a radio
transmitting a known code. Nothing else changes — the decoder already matches
against the table. `python3 src/dcs.py --check` prints 104/104 when it is right.

**Ham segments are ARRL national, not NESMC.** Correct for the country, wrong in detail
for Massachusetts. Every row carries a `source` column; re-seeding replaces them by
`(service, label, freq_lo_hz)`.

**Deviation measurement accuracy is unverified against real signals.** The FRS/GMRS rule
depends on it. The *estimator* is now checked against known synthetic deviations in
`--selftest` and lands within 4% of true peak, and its weak-signal behaviour is
characterised in section 3 — but nothing has measured a real transmitter through a real
receiver. Note how little that guarantee was worth by itself: the estimator was
accurate to 4% in isolation while reporting pure noise for every event the deck
actually logged, because the window it was handed in the field was not the window it
had been tested on (section 4). Phase 3 should transmit a known narrowband signal and a known wideband one, at
several distances, and compare against what the deck reports. If it is sloppy, raise
`DEV_EVIDENCE_MARGIN_HZ` or `DEV_MIN_SNR_DB` until false positives stop. Occupied
bandwidth is still measured by nothing; `events.bandwidth_hz` is NULL on every row the
deck produces.

---

## 6. Not recoverable from the repository

`rm -rf *` in the home directory on 2026-08-19 destroyed five design documents that were
never restored: `design-decisions.md`, `bench-bringup.md`, `rf-primer.md`,
`pi-architecture.md`, `phase1-detail.md`. The bench bring-up plan in particular is the
nine-phase gated procedure to follow the day the Airspys arrive — it exists only in the
original design conversations. Restore it before hardware lands.

`docs/band-plan-notes.md` was written but never installed, and its coverage section
describes the old 153.200 MHz VHF window rather than the current 154.950.

---

## 7. Housekeeping still outstanding

- README points at `tools/phase-log.md`; the file is `docs/phase_log.md`
- DHCP reservation for `radio-deck` — it moved .243 to .244 mid-session once already
- WiFi power save disabled via systemd oneshot (no NetworkManager on Ubuntu Server)
- Map physical USB ports to buses and label the case. The two Airspys must land on
  different 480M root hubs. Doable today with a thumb drive; retires a Phase 6 gate
- GitHub access is a full account key on a machine going into a backpack. A repo-scoped
  deploy key has the same convenience and a blast radius of one repo
- The repository is public. `data/` is gitignored so the database will not leak, but
  `docs/` will accumulate site notes and observed frequencies from real deployments
