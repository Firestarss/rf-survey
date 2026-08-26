# Tools

Everything in `tools/`, what it is for, and when you would reach for it. Two of
these are field instruments meant to be used without a laptop conversation —
`fieldsurvey.py` and `padcal.py` — and the rest are bench and build utilities.

Run all of them from the repository root.

---

## Field instruments

### `padcal.py` — how much attenuation does this site want?

**The value is not a property of the radio.** It depends on how much man-made
noise is arriving, which changes with the band, the antenna and where you are
standing. Phase 1 measured 4–6 dB at 466 MHz and 17–19 dB at 146 MHz on the same
afternoon with the same hardware — a factor of thirty in power. So it has to be
measured wherever the deck will actually stand.

```bash
python3 tools/padcal.py --serial <SERIAL> --freq 466.0e6 --ppm 0.64 --pad 20
```

`--pad` is what is **physically fitted right now**. The tool asks you to fit the
antenna, then swap to the dummy load, and works out what attenuation would put
the antenna-versus-dummy delta in the 8–10 dB window — the point where external
noise dominates the receiver's own without wasting headroom.

It sweeps several gains rather than measuring one, because two different
failures both look like "the floor did not move":

| | what is happening | what you see |
|---|---|---|
| below the ADC knee | the converter's own noise swamps everything | delta collapses to ~0 |
| above compression | the front end is out of headroom | floor stops rising, **clipping counters stay at zero** |

In the usable middle, equal steps of gain give equal steps of floor. The tool
reports which gains were linear and trusts only those. If none were, it says so
rather than producing a number.

**Takes about 3 minutes** at the defaults. Needs the radio, an antenna and a
dummy load — nothing else.

---

### `fieldsurvey.py` — turn a walk into a propagation model

Transmit from a series of points, each with a **different CTCSS privacy code**,
and the deck's own tone decode says which transmission was which. No stopwatch,
no notes on timing, no staying in contact with anyone — the recording identifies
itself. Collect coordinates however you like; a phone map is what produced the
first dataset.

Leave the deck running normally while you walk:

```bash
python3 src/survey_prototype.py --driver airspy --serial <SERIAL> \
    --freq 466.0e6 --rate 10e6 --gain 42 --ppm 0.64 \
    --db data/survey.sqlite --receiver-id uhf
```

**In the field**, to check it heard you at all:

```bash
python3 tools/fieldsurvey.py list --db data/survey.sqlite --freq 462.675e6
```

```
      time         MHz  code   CTCSS     dur     SNR   cap
  18:39:47    462.6750    21   136.5   8.76s   21.2  1.00
  18:40:47    462.6750    22   141.3   9.84s   29.5  0.71
```

**Afterwards**, with the coordinates, in a CSV with a header:

```
code,lat,lon,label
21,42.3848465,-71.0746755,corner by the school
22,42.3868687,-71.0776927,top of the hill
```

```bash
python3 tools/fieldsurvey.py fit --db data/survey.sqlite --freq 462.675e6 \
    --rx 42.3854086,-71.0796309 --points walk.csv
```

It prints distance, bearing, measured SNR, what the fitted model expected, and
the residual; then the path-loss exponent, R², and the range at which signals
fall to the detection threshold.

**Points you transmitted from and the deck did not hear still belong in the
file.** They carry most of the information. A miss where the model expected a
strong signal is an obstruction, not a range limit, and the tool calls those out
separately with their bearings so you can compare them against the paths that
worked.

#### Codes past 38 are DCS, and the numbering is the manufacturer's

Handhelds present one continuous list of "privacy codes", but it is two schemes
end to end: **1–38 are CTCSS tones**, and **39 upward are DCS codewords**. Where
the DCS part starts is standard; the order it runs in is not, and it varies by
manufacturer. So above 38, do not record the position in the menu — **record the
DCS number the radio displays** (023, 025, 026 …) and put it in a `dcs` column:

```
code,dcs,lat,lon,label
30,,42.1,-71.1,still CTCSS at code 30
,023,42.2,-71.2,DCS from here on
```

The tool keeps CTCSS and DCS in separate namespaces, so DCS 023 and privacy code
23 can both appear in one survey without colliding.

**Use `code`, not raw Hz, unless you are sure.** The tool carries its own 38-entry
privacy-code table. `survey_prototype.CTCSS_TONES` has 54 tones — every one the
decoder can identify, including several the 38-code scheme skips — so indexing
that by code number is wrong from code 2 upward, and would attribute every point
to the wrong location consistently enough to look like data.

#### Reading the fit

- **path loss per decade** — 20 is free space, 25–30 suburban, 30–40 dense urban.
  Well outside that range means something is wrong with the coordinates.
- **R²** — how much of the variation distance explains. Below about 0.8, look at
  the bearing column before trusting any range figure.
- **residual σ** — typical error of the model in dB. Under ~4 dB is a usable fit.

---

## Bench utilities

### `deck-check.sh` — diagnostics and soak

```bash
bash tools/deck-check.sh diag              # collect everything, ~20 s
bash tools/deck-check.sh soak [MINUTES]    # load test, default 10
bash tools/deck-check.sh all  [MINUTES]    # soak then diag
bash tools/deck-check.sh watch [MINUTES]   # sample without adding load
```

Reads only, apart from the soak, which runs `stress-ng` and cleans up after
itself including on ctrl-C. This is what to send when something is wrong:

```bash
bash tools/deck-check.sh all > phase0-$(date +%Y%m%d-%H%M).txt 2>&1
```

### `run-tests.sh` — the test suite

```bash
bash tools/run-tests.sh            # everything
bash tools/run-tests.sh -v         # per-test names
bash tools/run-tests.sh test_dcs   # one module
```

Stdlib `unittest`, no third-party dependencies, because it has to run on the deck
itself. **This is what "correctness" means in this project** — `--selftest` keeps
only sizing and speed, which is a property of the machine and the one thing no
unit test can answer. `RFSURVEY_SKIP_SLOW=1` leaves out the end-to-end test.

### `seed_band_plan.py` — populate the band plan

```bash
python3 tools/seed_band_plan.py data/survey.sqlite
```

FRS, GMRS, MURS and Part 90 channels plus ARRL band segments. Match windows are
**computed, not hand-written** — each is the smaller of half the authorised
bandwidth and half the gap to the nearest neighbour in the same service. It
checks for overlaps on every run and prints the count, which **must stay 0**.

Re-seeding replaces rows by `(service, label, freq_lo_hz)`, so it is safe to run
again after editing. Ham segments are ARRL national: right for the country,
wrong in detail for any particular coordination area.

### `make_fixtures.py` — the synthetic festival

```bash
python3 tools/make_fixtures.py data/survey.sqlite --wipe
```

A deterministic scenario with known answers, including deliberate traps: a
repeater decoy at the right offset with uncorrelated timing that must **not**
pair, the loudest channel in the set being narrowband so power cannot be used to
tell FRS from GMRS, and a distant handheld whose weak keyups measure wide so the
SNR gate on deviation has something to fail against.

`--wipe` clears `events` first. It does not touch `channels.notes`, which is
hand-written and deliberately survives rebuilds.
