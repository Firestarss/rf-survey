# Handoff — state of the software as of 2026-08-27

Written for whoever picks this up next, agent or human. Covers what exists, why it is
shaped this way, and what is deliberately unfinished. Read this before changing the
schema or the enricher; several things that look arbitrary are load-bearing.

Both Airspys arrived 2026-08-25 and Phase 1 is in progress. Sections 8 and 9 cover what
plugging one in found; `docs/phase_log.md` has the measurements and section 10 has what is
left to do.

**Be precise about what has and has not met a real signal.** `--spectrum` has, extensively,
and six faults came out of it. **The capture loop has, as of 2026-08-27** — detection,
event boundaries and CTCSS decoding are all confirmed against a real transmitter, and two
blockers came out of it, in section 10.

Still untouched by a real signal: **DCS decoding, the tier ladder, `enrich.pair()` and the
scoring path**. Those have only ever run against `simradio`, and the DCS module's history
in section 5 is a warning about how convincingly a decoder can be wrong while passing
every check it can run on itself.

Treat every threshold in this repository as a guess until Phases 3 and 4 say otherwise.

---

## 1. What is in the repository

```
src/survey_prototype.py   detector. Writes through db.py; owns no schema of its own.
src/dcs.py                Golay(23,12) and DCS codewords. Read its docstring first.
src/simradio.py           synthetic Airspy, so the capture loop runs with no radio
src/db.py                 connection, schema init, run lifecycle, log_event
src/schema_v2.sql         the frozen v2 baseline. Not the current shape.
src/migrate.py            v3-v8, and where every future change goes.
src/bandplan.py           frequency -> label lookup
src/enrich.py             tag, rollup, pair, score
src/cli.py                the two lines every command-line entry point shares
tools/seed_band_plan.py   FRS/GMRS/MURS/Part 90 channels + ARRL ham segments
tools/make_fixtures.py    deterministic synthetic festival scenario
tools/deck-check.sh       soak and diagnostics
tools/fieldsurvey.py      walk a site, fit path loss from CTCSS-tagged transmissions
tools/padcal.py           measure the attenuation this site and antenna want
profiles/festival.yaml    receiver assignments, detection thresholds, operator licences
systemd/                  unit file and deployment notes for unattended running
docs/phase_log.md         gate tracker. Phase 0 PASS.
docs/bench-bringup.md     the nine-phase gated procedure. Read this on delivery day.
docs/phase1-detail.md     Phase 1 step by step, headless
docs/design-decisions.md  what was chosen, what was rejected, why
docs/pi-architecture.md   where things live on radio-deck
docs/rf-primer.md         the radio concepts
docs/band-plan-notes.md   window choices and their reasoning
```

Run the whole chain with no hardware:

```bash
python3 src/db.py data/survey.sqlite
python3 tools/seed_band_plan.py data/survey.sqlite
python3 tools/make_fixtures.py data/survey.sqlite --wipe
python3 src/enrich.py data/survey.sqlite --profile profiles/festival.yaml
python3 src/survey_prototype.py --selftest      # sizing and speed only
bash tools/run-tests.sh                        # correctness
```

And the capture loop itself, still with no hardware — this drives the detector, the
analyser, the two-phase write and the retune logic end to end:

```bash
python3 src/survey_prototype.py --simulate 14 --receiver-id uhf
python3 src/survey_prototype.py --simulate 8 --rate 2.4e6 --receiver-id vhf \
        --dwell-seconds 6                       # rotation across both windows
python3 src/dcs.py                              # DCS codeword table self-check
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

`schema_v2.sql` stays v2-shaped. It stamps v2 and `init_schema()` replays every migration on
top, so adding a column there too makes the matching ALTER fail with "duplicate column
name" on every fresh build. The two comments claiming otherwise are corrected. Fresh and
upgraded databases were diffed schema-for-schema and are identical.

**The prototype conforms.** `open_db()` and the private `coverage` table are gone.
`coverage` said nothing `runs` and `run_receivers` do not, once `center_hz` existed, and
nothing joined to it. The two-phase write lives in an `EventLog` class holding every
field mapping in one place, which is what lets the tests drive the real code path
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
  they happened. Verified in `tests/test_tracker.py`: a 0.80 s signal logs as 0.80 s.

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
- **DCS is now decoded** (2026-08-19, section 5). A channel with a codeword reaches
  tier 3. `dcs_suspected` survives only for the case where something subaudible is
  present, is demonstrably not CTCSS, and no codeword comes out — that still caps
  at tier 2.
- **Transmissions shorter than the analysis dwell are never fully analysed.** Most
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
        --dwell-seconds 6            # exercises rotation across both windows
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

`systemd/rfsurvey@.service` runs one instance per receiver. `Restart=on-failure`,
not `always`: the survey is something you start and stop, the Pi has other uses,
and a deck that comes back by itself after you deliberately stopped it is worse
than one that does not run at all. A crash or a non-zero exit still restarts —
that is the case worth recovering. `KillSignal`
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

**DCS decoding is finished.** This was an open question and is now closed; kept
here because the way it failed is instructive.

The module shipped with the wrong Golay generator polynomial. 0xC75 and its
reciprocal 0xAE3 both generate a perfect binary Golay code, both round-trip, both
correct three bit errors, and both satisfy every internal consistency check the
code can run — so every codeword it produced was a valid codeword of the wrong
code, and nothing self-contained could have noticed. It took one decoded off-air
word to settle it: DCS 023 is `100 000010011 11101100011`, and that single
reference is now a test.

The apparent ambiguity was also a misreading. Because the code is cyclic and
all-ones is a codeword, every rotation and complement of a codeword is a codeword,
which looked like it made blind framing impossible — and with the wrong polynomial
and a guessed code list, 61 of 104 codes appeared undecodable. With the right
polynomial and the real 112-code list, every waveform has exactly **two** legal
readings, one normal and one inverted, and they are the same signal: transmitting
023 normal *is* transmitting 047 inverted, and a radio set to either opens on it.
That is the inverted-code pairing radio documentation lists. `dcs.INVERTED_PAIR`
derives it from the codewords and asserts on import that every code pairs cleanly,
so a future edit to the table that breaks the property fails at import rather than
in the field.

The decoder reports the normal reading, which every waveform has exactly one of.
Measured against synthetic signals: every code decodes at 0.9 s and 1.4 s dwell,
in both polarities, down to ~17 dB in-channel SNR, with zero wrong codes, zero
CTCSS tones misread as DCS, and zero decodes from pure noise. `python3 src/dcs.py`
prints the table's self-check.

**Ham segments are ARRL national, not NESMC.** Correct for the country, wrong in detail
for Massachusetts. Every row carries a `source` column; re-seeding replaces them by
`(service, label, freq_lo_hz)`.

**Deviation measurement accuracy is unverified against real signals.** The FRS/GMRS rule
depends on it. The *estimator* is now checked against known synthetic deviations in
`tests/test_analyze.py` and lands within 15% of true peak, and its weak-signal behaviour is
characterised in section 3 — but nothing has measured a real transmitter through a real
receiver. Note how little that guarantee was worth by itself: the estimator was
accurate to 4% in isolation while reporting pure noise for every event the deck
actually logged, because the window it was handed in the field was not the window it
had been tested on (section 4). It then measured excursion from the *channel grid*
rather than from the carrier, which added the transmitter's offset from its grid slot
to every answer (section 6). Phase 3 should transmit a known narrowband signal and a known wideband one, at
several distances, and compare against what the deck reports. If it is sloppy, raise
`DEV_EVIDENCE_MARGIN_HZ` or `DEV_MIN_SNR_DB` until false positives stop. Occupied
bandwidth is still measured by nothing; `events.bandwidth_hz` is NULL on every row the
deck produces.

---

## 6. Third pass: a code review of the whole repository

Nothing here was found by running the deck. It came from reading every file and
then checking the readings against measurements, which is why most of it is
small and two items are not.

### The two that matter

**Deviation was measured from the channel grid, not from the carrier.**
`p99(|inst|)` left the DC term in, and that term is however far the transmitter
sits from the 6.25 kHz slot it was filed under:

```
carrier off grid    0 Hz     1250 Hz    2500 Hz    3125 Hz
reported (2.4 kHz)  2326      3539       4788       5414
```

Eleven of the 85 seeded channels are off-grid by 1250–2500 Hz — **every MURS
channel**, several Part 90 VHF dots, and 146.520 — so a narrowband signal on one
of them read wide, and wide is the verdict that rules FRS out. The FRS and GMRS
channels themselves are all on-grid, so the discriminator was not mislabelling
in practice; the number in `channels.deviation_hz` and `v_contactable.dev_hz`
was wrong on the VHF channels, and it reads as evidence. `freq_error` is
subtracted before the percentile now. 146.520 went 3613 → 2448 Hz in the capture
path. Any deviation figure recorded before 2026-08-19 on those channels is high
by its grid offset.

**A burst of interference in the pretrigger defeated the analysis trim.** The
trim took its edges from the first and last sample over threshold, so anything
early in the lead-in anchored it at the start of the window and the whole
carrier-free run came back in — deviation ~11500 Hz, the noise figure, which is
section 4's bug arriving through a different door. 0.05 ms of interference is
enough; a single sample is not, because the decimation filter absorbs it. The
envelope is smoothed before thresholding and the edges come from a percentile of
the crossings. Regression test in `tests/test_analyze.py`.

### Things that silently did nothing

- `deck-check.sh` looked for `survey_prototype.py` in four places, none of them
  `src/`, so the Phase 0 selftest never ran and printed "not found" instead.
- `detection.min_duration_s` and `detection.hang_s` were in the profile and read
  by nobody. `EventTracker` used its own defaults, which happened to be the same
  numbers — so editing the profile changed nothing and said nothing. Same for
  `receivers.*.attenuator_db` and `.antenna`, which have had columns since v2
  and were NULL on every real run.
- `conn.commit()` in the capture loop was a no-op. `db.connect` opens with
  `isolation_level=None`, so every statement commits as it executes; the calls
  read as transaction boundaries and were not.
- `migrate.apply()` stamped the target version when no migration reached it,
  which would mark a database as upgraded by a step that does not exist.
- `festival_scenario` had traffic for one of the three VHF windows, so the
  rotation command in section 1 logged one event and two windows of silence
  indistinguishable from a broken detector. Every window has traffic now and
  `--simulate` announces what each one can hear.
- The tier 0 fixture capped keyups at 1.0 s, written when the analysis dwell was
  1.4 s. The dwell is 0.9 s now, so 464.500 had moved to tier 1 while every
  document still called it the tier 0 case. It tracks `ANALYZE_SECONDS`.

### Shape

`run()` was 432 lines and four levels deep, so the only way to exercise any of
it was to run the whole loop. It is now `resolve_settings`, `Radio`, `Detector`
and `CaptureLoop`, split along the seam the retune already implied — console
output and database rows are identical on both simulate runs.

`selftest()` was 312 lines and correctness had migrated into `tests/`
underneath it. It keeps sizing and speed, which is a property of the machine
and the one thing no unit test can answer; **`bash tools/run-tests.sh` is
correctness now**, and `deck-check.sh` runs both. Three checks that existed only
in the selftest moved into the suite first.

`schema.sql` is `schema_v2.sql`. It describes a database that has not existed
since v2 and calling it the schema invited reading it as one.

The end-to-end test drove the capture loop from `setUp`, so 24 methods meant 24
identical runs — 87 s of the suite's 147 s. One run per class now; the suite is
68 s.

Deleted: `tools/apply-v2.sh` and `apply-v4.sh`, 326 lines describing how to
upgrade to schema versions four and five behind current.

---

## 7. The design documents — restored

`rm -rf *` in the home directory on 2026-08-19 destroyed five design documents. **They were
restored and are in the repository**, tracked as of b304e79:

```
docs/bench-bringup.md     788 lines   the nine-phase gated procedure. Read on delivery day.
docs/phase1-detail.md     450 lines   Phase 1 broken out, desktop assumptions removed
docs/design-decisions.md  342 lines   what was chosen, what was rejected, why
docs/pi-architecture.md   334 lines   where things live on radio-deck
docs/rf-primer.md         270 lines   the radio concepts, for a systems reader
```

An earlier version of this section said they were lost and had to be restored before
hardware landed. That was already untrue when it was written — they were restored in the
same commit. Corrected 2026-08-19 after checking the machine rather than the note.

`docs/band-plan-notes.md` is installed and current: its coverage section gives the VHF
window as 154.950 with the reasoning, not the old 153.200. That warning is also cleared.

**`bench-bringup.md` carries its own copy of the phase table**, and it does not know about
anything in sections 3, 4 or 6 above. Its Phase 0 line still reads "28.8% of one core",
which was measured before short transmissions were analysed at all, before DCS decoding,
and before captures were written to disk. Re-measure before trusting it — see section 5.
Its Phase 0 procedure now names both `--selftest` and `tools/run-tests.sh`, because as
of section 6 the selftest no longer checks correctness.

---

## 8. Fourth pass: --spectrum, and the first hardware

The hardware landed on 2026-08-25. Reading `phase1-detail.md` against the code
first — before plugging anything in — found that three of the four numbers Phase 1
exists to produce could not be obtained with the tool the procedure names.

The common cause is structural and is worth stating plainly. `spectrum_capture`
imports SoapySDR directly instead of going through the `Radio` wrapper that
exists precisely so `--simulate` can substitute itself (`survey_prototype.py`,
`Radio.__init__`). So `--spectrum` was the one path the simulator could not
reach, and like `run()` in section 4 it had never been executed. Everything
below was found by standing a stub SoapySDR module in front of `simradio` and
running it. **It is the same lesson as sections 4 and 6, arriving through a
third door: the code that cannot be exercised is the code that is wrong.**

### What running it found

**The band reference level was never printed.** Steps 6 and 11 of the bench
procedure both say to record it, and Gate 1's antenna-versus-dummy delta is the
entire gain-setting method — raise gain until the antenna lifts that level
8-10 dB. It was computed and used only to subtract for SNR, and since the peak
list quotes SNR *relative* to it, the absolute level appeared nowhere in the
output. There was no way to perform the gain step as written.

**The peak list reported every skirt.** An FM transmission at 2.5-5 kHz
deviation spans about 11 kHz against a 6.25 kHz grid, so each carrier lights its
own channel and both neighbours. The live detector has required a local maximum
since section 4; `find_peaks` never got the same rule. Measured on a simulated
step 8 sweep: **seven keyups printed twenty-one entries.** Gate 1 asks the
operator to account for every entry in the list, and the `top=25` cap meant
skirts of strong signals displaced genuinely weak ones — the exact entries step
13 exists to find.

**Step 12 could not measure ppm at all, and returned a confident zero.** The
peak list reported `channels * CHANNEL_HZ`, quantised to 6.25 kHz. The procedure
tunes a generator to exactly 466.000 MHz, which sits precisely on a slot, and
expects to resolve the ±470 Hz that is ±1 ppm — so every error under ±3125 Hz
rounded to 466.0000 and read as 0.0 ppm. ppm is one of the three numbers Phase 1
exists to produce and it feeds `--ppm`, the profile, and every later phase.

`refine_peak_hz` now interpolates a parabola through the peak FFT bin and its two
neighbours, in dB, and the peak list carries a measured frequency, an offset in
Hz and a ppm figure per entry. Measured against known offsets through the full
capture path:

```
true offset     measured     error     as ppm
          0          0.0      +0.0     +0.000
        120        128.0      +8.0     +0.017
       -300       -320.0     -20.0     -0.043
        470        512.0     +42.0     +0.090
       1200       1216.0     +16.0     +0.034
       2400       2400.0      +0.0     +0.000
```

Worst 42 Hz, or 0.09 ppm at 466 MHz — an order of magnitude inside the ±1 ppm
gate, and stable down to 12 dB SNR. Bins are 2441 Hz at 10 MSPS, so this is
resolving a fiftieth of a bin.

**A dedicated high-resolution ppm mode was considered and rejected.** It would
measure the *difference between two clocks* more precisely, and the procedure's
own caveat is that the MiniSA's reference oscillator is plausibly worse than the
Airspy's — so the extra precision lands entirely on the unknown. Use the NOAA
transmitters at 162.400-162.550 MHz as the accuracy cross-check instead, as the
procedure already suggests; ppm is constant across frequency, so a reading there
applies at 466 MHz. The independent second opinion is `freq_raw_hz - freq_hz` per
event, which measures the same thing through the real capture path rather than a
diagnostic side-path. Two roughly-agreeing numbers from different paths are worth
more than one very precise number from one. Revisit only if those disagree.

The per-entry ppm column also makes step 8's evenly-spaced test real. That check
distinguishes a clock offset, which is benign, from a sample-rate fault, which
the procedure rightly calls much more serious — and on the grid alone it could
never have failed, because all seven channels always snapped to nominal.

### Tests

`tests/test_spectrum.py`, 10 tests. It carries the stub-SoapySDR harness, so
`--spectrum` is now reachable without hardware and the next change to it is
checked rather than inspected. The suite was 211 tests at that point; it is 222 now.

### Documentation corrected

- Every bench command said `python3 survey_prototype.py`; it is in `src/`. The
  same mistake `deck-check.sh` made in section 6.
- `collect-diag.sh` does not exist and never has. The collector is
  `bash tools/deck-check.sh diag`.
- The host is `radio-deck`, not `surveydeck`.
- `bench-bringup.md`'s sample output block predated all of the above and showed
  a peak list that the code could not produce.

### Then the radio was plugged in, and found three more

The first Airspy went in that evening. Everything above was still synthetic;
these came from the hardware within the hour, and the first one is the reason
none of the rest could have been found earlier.

**`SoapySDR.Device({"driver": "airspy"})` does not work.** A plain Python dict
raises `make() no match` on the 0.8.0 bindings Ubuntu 26.04 ships; the string
markup `"driver=airspy,serial=..."` works, as does the `SoapySDRKwargs` that
`enumerate()` returns. Both hardware call sites built dicts, so **the deck could
not open a radio at all** — and the error is indistinguishable from no radio
being present. `--simulate` substitutes `SimulatedRadio` for the entire `Device`
call, so no amount of running without hardware could have reached the line. Now
in `device_args()`.

Serial addressing was verified against the hardware at the same time: matching
is case-insensitive and tolerates a leading `0x`, and a serial matching nothing
is refused rather than silently opening whatever is attached. Section 2's
"address by serial, never by index" holds.

**`--spectrum-seconds` ran for exactly half the time it was given.**
`readStream` returns whatever the driver's transfer size is and ignores the
count asked for — this Airspy returns 65536 samples against the 131072
requested, every single call. The loop counted each return as one full frame,
so a 120 s sweep closed after 60 s of signal. Nothing in the output disclosed
it, because the summary line printed the duration that had been *requested*.

It was found because the operator noticed the window felt short when a
seven-channel bench sweep kept losing its last channels — not by any check in
the program. The loop now runs on samples delivered, and every line that
mentions a duration reports what actually arrived.

**Gain is not the control the bench procedure describes.** `phase1-detail.md`
said the driver exposes a "linearity" setting from 0 to 21. It does not: this
module exposes an overall 0–45 dB that fills three stages **in sequence** — LNA
0–15, then MIX 0–15, then VGA 0–15. Measured on a 50 ohm load at 466 MHz:

```
--gain   30     33     36     39     42     45
VGA       0      3      6      9     12     15
floor  -131.1 -130.9 -128.4 -121.8 -112.1 -102.3 dB
```

Flat to gain 33 and then rising about 10 dB per three steps, so the VGA numbers
are index steps rather than dB. Below the knee at ~36 the receiver is
**ADC-noise-limited**: the front end is amplifying but its noise is still under
the converter's own floor. That is why the profile's `gain: 12` — LNA only,
nothing else — read a handheld ten feet away at 9.6 dB SNR, and the same radio
at gain 42 read it at 48.6 dB.

**Step 11 cannot work below that knee at all.** Its method is to raise gain
until the antenna lifts the noise floor 8–10 dB over a dummy load, and below
gain 33 the floor is pinned by the converter and no antenna would move it.

### Memory, once captures ran their full length

Fixing the duration doubled the frame count and exposed the next ceiling: a 60 s
capture peaked at **3.1 GB** resident, which projects to roughly 15 GB for the
300 s capture step 13 asks for. The peak and average traces are running
statistics that never needed history, and the waterfall needs at most a
screenful of rows. Both are accumulated now, with waterfall rows **max-pooled**
onto a stride that doubles as the buffer fills — pooled rather than sampled,
because thinning would alias away a keyup shorter than the stride, which is
exactly what the picture is read for. Same 60 s capture: **1.0 GB, and 65 s wall
clock against 78 s.**

### What the first real spectrum showed

Boston metro, dummy load, gain 42. Distinguishing the receiver's own artefacts
from a genuinely busy band turned out to have a clean test that costs nothing:

**An internal spur is coherent with the receiver's own reference, so it reports
zero frequency offset. A real transmitter shows the receiver's clock error.**
470.000000 MHz reads `+0 Hz` on every capture and is internal. 464.000000 MHz
reads −0.6 to −0.8 ppm — the deck's own clock error — and is a real transmitter
on a legitimate Part 90 frequency. The strong wandering signals around 468.1 and
468.5 are Boston UHF business traffic, different transmitters at different
moments, which is why they appear at a different frequency in every capture.

That test only exists because the peak list reports a measured frequency, which
it did not until the same day. Three of these sit above `detection.on_db` of
10.0 dB, so the deck as configured would log them as traffic.

**The band-wide floor breaks down once the passband is visible.** `find_peaks`
compares every channel against one scalar median. At gain 12 the ADC's flat
noise hid the analog response; at gain 42 the real shape appears — rolled off
below 461.5 and above 470.3, with a raised shoulder at 470.3–470.6 that
manufactures a cluster of +6 dB entries that are not signals. A floor that
follows frequency while still ignoring narrow carriers is the fix. Not yet done.

### Still true and still unmeasured

Nothing here has seen a real signal either. Every number above came from
`simradio`, which has no path loss, no multipath and no adjacent-channel
splatter, and its "carrier" for the ppm work is a mathematically exact complex
exponential. It says the arithmetic is right. It cannot say the receiver is.

---

## 9. Fifth pass: compression, and regrouping the receivers

Both changes fall out of the same measurement, and the first is a failure mode the deck
had no way to see.

**Gain compression is invisible to `OverloadMonitor`.** It watches two things: clipping,
which is samples at full scale, and desense, which is the floor on every channel rising
together. Compression is neither. It sets in well before samples reach full scale — so
clipping frames read **zero** right through it — and it makes the floor fail to *rise*,
which is the opposite of what desense looks for. Every indicator stays clean while every
level the deck logs is understated.

Measured at 146 MHz on a bare antenna, the floor rises +10.4 dB and +10.2 dB across the
first two gain steps, then only +6.2 and +2.5. `compression_verdict()` compares two equal
gain steps and asks whether they agree, which is self-calibrating — necessary, because the
Airspy's "dB" of gain are index steps and three of them move the floor about ten, so any
absolute expectation would be device lore. It returns `linear`, `compressed`, or
`inconclusive`; the last is the honest answer below the ADC knee, where neither step moves
and there is nothing to compare. Calling that "linear" would be wrong in the most dangerous
direction — a clean bill of health for a configuration the check cannot assess.

It runs **per window**, not per run, because the answer depends on what is on the air in
the band being listened to: the same receiver at the same gain measured linear at 466 MHz
and compressed at 146 MHz minutes apart. Migration 9 records the verdict on
`coverage_windows` and `v_coverage` surfaces it, for the same reason `events.overload`
exists — a coverage figure that does not say the band was compressed is exactly the
clean-looking log full of junk the overload monitor was built to prevent.

**The receivers are now grouped by required attenuation, not by service.** Phase 1 measured
446 and 466 wanting 4–5 dB and 146 and 155 wanting 17–20 dB. A receiver carries one pad, so
grouping ham with ham put a 4 dB need and a 20 dB need on the same radio, which nothing
satisfies: too little attenuation compresses the front end silently, too much throws away
the sensitivity the survey depends on. 446 moved to `uhf`.

That made `uhf` rotate, which would have halved coverage of the band the survey exists for,
so **dwell is per window now**, falling back to the receiver's: 466 gets 300 s and 446 gets
60. `--dwell-seconds` still overrides everything, so `--simulate` keeps exercising rotation
quickly.

---

## 10. What remains

Ordered by what it needs rather than by phase number, because the blocking constraint is
usually a part in the post rather than a gate.

### The capture loop has now met a real signal — see `docs/phase_log.md` Phase 2

Opened 2026-08-27. Detection and logging work: correct frequencies, correct event
boundaries, CTCSS decoded off the air at capture ratio 1.0, and short transmissions
correctly refused a tone rather than given a made-up one. Two blockers stand between that
and Gate 2, and both belong on this list rather than buried in the phase log.

**The loop saturates one core at 10 MSPS.** 95-98% of a core, single-threaded, achieving
124 reads/sec where real time needs 152.6 — and the 19% shortfall is the overflow.
Gate 2's CPU check reads 24.6% "across four cores" and passes while the binding resource
is pinned, so the gate measures the wrong thing. 10 MSPS is not negotiable: repeater
pairing needs 462.x and 467.x heard together, 5 MHz apart. Moving analysis off the read
thread is the obvious candidate, since one 78 ms `analyze_analog` blocks twelve reads.

**Strong signals manufacture phantom events that every recorded field says are real.** A
handheld at 10 feet produced 124 phantoms across the whole span. Most were broadband
desense and were correctly flagged `overload`; what survives at lower gain is worse. They
are odd harmonics of the carrier's baseband offset from the tuned centre — exact odd
integers, verified — so they inherit the parent's CTCSS tone at full confidence, match its
duration to 14 ms, and are **not** flagged, because `OverloadMonitor` watches for a
broadband lift and these are discrete products.

The discriminator exists and is not yet implemented: a phantom's `freq_raw_hz - freq_hz`
is exactly N times its parent's, because the harmonic multiplies the offset error along
with the offset. A real transmitter's frequency error has no relationship to its distance
from the deck's tuned centre. What to *do* with such an event — drop it, flag it, or
record the parent it derives from — is a design decision, not a coding one.

Also note **the linearity check from section 9 cannot see this**. It runs once when a
window opens, so compression caused by an intermittent strong signal is invisible to it;
it reported `linear` for the window in which all 124 phantoms appeared, and was right at
the moment it looked.

### Siting beats sensitivity

The first real propagation data, 2026-08-27, is in `docs/phase_log.md` Phase 2: a
5 W handheld at seven surveyed points, each identified by its own CTCSS so the
deck's decode says which transmission it was. Path loss fits 28.9 dB per decade,
normal urban clutter, and the chain is usable to about 1.1 km at `on_db` 10.

The number that matters is not the range. **One building between transmitter and
receiver cost more than 20 dB — more than quadrupling the deck's sensitivity
would buy back.** 412 m delivered 21.2 dB and 492 m delivered nothing at all.

So a single deck cannot be assumed to cover a site by radius, and the honest
coverage claim is line-of-sight. Where the antenna stands at the festival will
matter more than any threshold tuned on the bench — which is worth knowing
before spending a deployment learning it.

### The next largest untested surface

**Tone and DCS decoding beyond two tones, the tier ladder, and repeater pairing.** Two CTCSS tones have now been decoded off the air, but the DCS decoder, the
tier ladder, `enrich.pair()` and the whole scoring path have still only ever run against
`simradio`. Phase 3 needs nothing that is not already on the bench.

### Then, still with nothing new to buy

- **Phase 3, tones.** CTCSS and DCS against real radios. The decoder has only met synthetic
  tones, and section 5 records how convincingly the DCS module was wrong before one
  off-air codeword settled it.
- **Decide what to do about the internal spurs.** 464.000 and 470.000 MHz sit above
  `detection.on_db` of 10.0 and will log as traffic on channels nobody keyed. They are
  identifiable — a spur generated from the deck's own reference is coherent with it and
  reports a zero frequency offset, where a real signal shows the receiver's clock error —
  so masking them is tractable. Doing nothing means a festival log with invented events in it.
- **Housekeeping**: DHCP reservation for `radio-deck`, which moved .243 → .244 mid-session
  once already; WiFi power save off via a systemd oneshot, there being no NetworkManager on
  Ubuntu Server; and labelling the USB ports. Radio 1 is on Bus 004 Port 1 and the second
  must land on a different 480M root hub, which retires part of a Phase 6 gate.

### Blocked on parts

- **A 5 dB pad for `uhf` and a 20 dB for `vhf`**, then re-verify the delta and close Gate 1
  steps 10, 11 and 13.
- **Re-measure 155 MHz once it is linear.** It is uncharacterised, not fine: a paging
  transmitter at 152.600 reading +55.4 dB drives the front end in and out of compression
  between captures, and no antenna-versus-dummy figure taken in that state means anything.
- **A second FM notch and a second antenna.** One Flamingo cannot serve two receivers, and
  broadcast rejection matters *more* on the VHF radio — 146 MHz is 38 MHz from the top of
  the broadcast band and that receiver is already the one compressing.
- **Phase 6**, second radio: serial, ppm, gain and its own linearity check, both radios on
  different 480M root hubs under load.

### Before it is deployable

- **Phase 4** — 24 h on one radio, which is also the first honest look at whether
  `detection.on_db` and `off_db` are right. Every threshold in this repository is still a
  guess made against synthetic signals.
- **Phase 7**, repeater matching, against real traffic. The first survey already caught
  5 MHz splits at 461.2/466.2, so the material exists.
- **Phase 8**, 24 h with everything running.
- **Disk budget.** Captures run ~16 kB/s of audio and ~190 kB/s of IQ *of traffic*, and a
  festival is measured in days. `--capture-mb` caps it, but the cap wants choosing against
  a real event rather than a guess.
- **Power.** Not addressed anywhere in this repository, and a Pi 5 with two SDRs in a
  backpack for a weekend is a real problem. Worth solving before the site, not at it.

### Deliberately not scheduled

- **`content` is still never determined.** Section 5 explains why, and that reasoning
  stands. What has changed is that `--capture-dir` now exists, so a real deployment finally
  produces recorded audio to revisit it against rather than more synthetic signals.
- **GitHub access is a full account key** on a machine going into a backpack. A repo-scoped
  deploy key has the same convenience and a blast radius of one repository.
- **The repository is public.** `data/` is gitignored so the database will not leak, but
  `docs/` will accumulate site notes and observed frequencies from real deployments.
