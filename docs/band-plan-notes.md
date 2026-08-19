# Band plan notes

Where the numbers in `band_plan` came from, how the lookup resolves, and what the
receiver windows actually cover. Current as of 2026-08-19.

---

## 1. What is seeded

`tools/seed_band_plan.py` writes 117 rows: 82 discrete channels and 35 ham segments.

| Service | Rows | Kind | Source |
|---|---|---|---|
| FRS | 22 | channel | 47 CFR 95B, post-2017 rules |
| GMRS | 30 | channel | 47 CFR 95E — 22 channels + 8 repeater inputs |
| MURS | 5 | channel | FCC MURS page, frequencies and bandwidths verbatim |
| Part 90 itinerant | 23 | channel | dot/star channels, cross-checked across three sources |
| 2 m ham | 19 + 3 | segment + channel | ARRL national plan |
| 70 cm ham | 13 + 2 | segment + channel | ARRL national plan |

FRS and GMRS share 22 frequencies deliberately. The two services have different power
limits, bandwidths and licence status on the same channel, so a detection is labelled
with both possibilities rather than one arbitrary pick — see §4.

**Three "dot" channels are GMRS, not Part 90.** White Dot (462.5750), Black Dot
(462.6250) and Orange Dot (462.6750) are GMRS 16, 18 and 20. Cheap radios ship with
them preprogrammed under the dot names, so expect vendor and event-staff traffic.

**Blue Dot and Green Dot are MURS**, moved out of Part 90 in 2002.

**Uncertain:** K Dot is 467.8125 by two sources and 457.8125 by a forum post. Took the
majority. Low stakes — if nothing is ever heard there, it was wrong.

---

## 2. Everything is a range

`band_plan` stores `freq_lo_hz`/`freq_hi_hz`, never a bare frequency. A discrete channel
is a narrow range around its nominal centre; a ham segment is a wide one. One containment
query serves both.

This exists because a detection never lands exactly on nominal — ppm error and FFT bin
resolution put it a few hundred Hz off. Storing ranges puts the match tolerance in the
data where it can be seen and tuned, instead of hiding it as a constant in the enricher.

Lookup returns every containing range ordered narrowest first, so specific beats general:

```
146.5200 MHz — 2 m national FM simplex calling, +0.00 kHz from nominal; within 2 m simplex
146.5194 MHz — 2 m national FM simplex calling, +0.60 kHz from nominal; within 2 m simplex
146.4700 MHz — 2 m simplex
145.3300 MHz — 2 m FM repeater outputs
462.6748 MHz — FRS 20 / GMRS 20, +0.20 kHz from nominal
```

**Channel windows are computed, not hand-written.** Each half-window is the smaller of
half the authorised bandwidth and half the gap to the nearest neighbour in the same
service. FRS primary and interstitial channels interleave to 12.5 kHz spacing, and
151.505 sits only 7.5 kHz from 151.5125 — hand-written tolerances would have overlapped
silently. The upper bound is one Hz short of the midpoint, because both bounds are
inclusive and adjacent channels would otherwise share an endpoint.

`seed_band_plan.py` reports the overlap count on every run. **It must stay 0.**

---

## 3. Receiver coverage

Seeded channels against the profile's windows at 10 MSPS:

| Window | Centre | Nominal span | Channels |
|---|---|---|---|
| `uhf` parked | 466.000 | 461.000–471.000 | 40 |
| `vhf` rotating | 446.000 | 441.000–451.000 | 1 |
| `vhf` rotating | 146.000 | 141.000–151.000 | 3 |
| `vhf` rotating | 154.950 | 149.950–159.950 | 14 |

The ham windows show low channel counts because ham allocations are mostly segments,
which this count excludes; both windows fully contain their bands.

**154.950 was chosen by search, not by eye.** It is the midpoint of the 151.505–158.400
span of VHF business and MURS channels, covers all 14 even at a conservative 8.5 MHz
usable bandwidth after filter rolloff, and has nothing sitting on the centre frequency
where the LO artifact lands. The previous value of 153.200 missed the 158.400 itinerant
by 200 kHz.

**Five channels fall outside every window**, unchanged whether you assume the full
10 MHz or a conservative 8.5 MHz:

| Frequency | Channel | Note |
|---|---|---|
| 432.1000 | 70 cm calling frequency | SSB/CW, 14 MHz below the 446.0 window |
| 451.8000 / 451.8125 | Itinerant | ~800 kHz above the 446.0 window |
| 456.8000 / 456.8125 | Itinerant inputs | ~5.8 MHz above |

The 451.8/456.8 pair is a genuine itinerant repeater pairing used by event production
crews — close to the exact traffic this deck exists to find. Reaching it needs a fourth
rotation window near 454 MHz, which costs dwell on the three that exist. Deferred until
a real deployment shows whether that band is busy.

432.100 is a deliberate miss. It is SSB/CW weak-signal work, not FM voice, and the
detector is built for the latter.

---

## 4. Telling FRS from GMRS on a shared channel

Only channels 15–22 offer any leverage:

| Channels | FRS power | GMRS power | FRS bandwidth | GMRS bandwidth |
|---|---|---|---|---|
| 1–7 (462 interstitial) | 2 W | 5 W | 12.5 kHz | 12.5 kHz |
| 8–14 (467 interstitial) | 0.5 W | 0.5 W | 12.5 kHz | 12.5 kHz |
| 15–22 (462 main) | 2 W | 50 W | 12.5 kHz | **20 kHz** |

Channels 8–14 are identical in every respect — no discriminator exists. Channels 1–7
differ by 4 dB, which is noise.

**Received power is not usable, on any channel.** It is transmit power minus path loss,
and path loss swings tens of dB across a site. At a festival the correlation likely
inverts: FRS handhelds are dense and close, GMRS repeaters are on distant towers. The
deck also has a 20 dB attenuator fitted and an unrecorded antenna, so absolute power is
not calibrated to begin with.

**Bandwidth and deviation are usable**, because both are properties of the transmission
rather than the path. Anything measurably wider than 12.5 kHz on channels 15–22 cannot
be FRS. Deviation is the stronger form — FRS peak deviation is capped at 2.5 kHz against
GMRS wideband at roughly ±5 kHz — and it is the field `survey_prototype.py` actually
measures, so the discriminator should read `deviation_hz`.

**The inference is one-sided.** Wide rules FRS out; narrow rules nothing out, because
narrowband GMRS radios are common and look exactly like FRS.

Two cases are already definitive and need no measurement: the eight 467.550–467.725
inputs are GMRS-exclusive, and any repeater output the pairer matches is GMRS by
construction, since FRS is simplex-only.

`tools/make_fixtures.py` carries the control case for this — 462.650 is the loudest
channel in the synthetic set at 56 dB SNR and must stay labelled `FRS 19 / GMRS 19`
because it is narrowband. If a change ever promotes it to GMRS, that change is wrong.

**Unverified:** none of this has met a real signal. Phase 3 should transmit a known
narrowband and a known wideband source and compare against what the deck reports. If the
measurement is sloppy, raise `BW_EVIDENCE_MARGIN_HZ` until false positives stop.

---

## 5. Ham segments are national, not regional

ARRL states plainly that locally coordinated plans take precedence over the national
plan. For Massachusetts that means **NESMC**. The seeded 2 m and 70 cm segments are ARRL
placeholders — correct for the country, wrong in detail here. Every row carries a
`source` column; re-seeding replaces rows by `(service, label, freq_lo_hz)`.
