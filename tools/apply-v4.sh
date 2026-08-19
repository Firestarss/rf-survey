#!/usr/bin/env bash
# Apply schema v4 and the enrichment pipeline.
#
# Run from ~/rfsurvey AFTER scp-ing these into place:
#   src/migrate.py  src/enrich.py  src/db.py  src/bandplan.py
#   tools/make_fixtures.py  tools/seed_band_plan.py  profiles/festival.yaml
#
#   bash tools/apply-v4.sh
#
# schema.sql is unchanged — v3 and v4 arrive as migrations, applied in place.
# Verifies before it commits. If any check fails, nothing is committed.

set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok   $*"; }

echo "== 1. files present =="
for f in src/db.py src/schema.sql src/bandplan.py src/migrate.py src/enrich.py \
         tools/seed_band_plan.py tools/make_fixtures.py profiles/festival.yaml; do
    [ -s "$f" ] || fail "$f missing or empty"
    ok "$f"
done

echo
echo "== 2. syntax =="
python3 -m py_compile src/db.py src/bandplan.py src/migrate.py src/enrich.py \
    tools/seed_band_plan.py tools/make_fixtures.py || fail "python syntax error"
ok "python parses"
python3 -c "import yaml; yaml.safe_load(open('profiles/festival.yaml'))" \
    || fail "festival.yaml is not valid YAML"
ok "yaml parses"
grep -q '^SCHEMA_VERSION = 4$' src/db.py || fail "db.py is not at SCHEMA_VERSION 4"
ok "SCHEMA_VERSION 4"

echo
echo "== 3. migrating in place (no rebuild) =="
BEFORE=$(python3 -c "
import sqlite3
try:
    print(sqlite3.connect('data/survey.sqlite').execute(
        \"SELECT value FROM schema_meta WHERE key='version'\").fetchone()[0])
except Exception: print('none')")
echo "  before: v$BEFORE"
python3 src/db.py data/survey.sqlite

echo
echo "== 4. band plan =="
python3 tools/seed_band_plan.py data/survey.sqlite | tail -2

echo
echo "== 5. synthetic fixtures =="
python3 tools/make_fixtures.py data/survey.sqlite --wipe | sed -n '1p'

echo
echo "== 6. enrichment =="
python3 src/enrich.py data/survey.sqlite --profile profiles/festival.yaml

echo
echo "== 7. verifying =="
python3 - <<'PY' || exit 1
import sqlite3, subprocess, sys
db = sqlite3.connect("data/survey.sqlite"); db.row_factory = sqlite3.Row
bad = []

v = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
print(f"  schema version: {v}")
if v != "4": bad.append(f"schema says v{v}, expected 4")

cols = {r[1] for r in db.execute("PRAGMA table_info(events)")}
if "tone_state" not in cols: bad.append("events.tone_state missing")
else: print("  events.tone_state present")

# The repeater must be found; the decoy at the same offset must not be.
pairs = db.execute("""SELECT o.freq_hz o, i.freq_hz i, p.confidence, p.median_lag_s
                      FROM pairs p JOIN channels o ON o.id=p.output_channel_id
                      JOIN channels i ON i.id=p.input_channel_id""").fetchall()
print(f"  repeater pairs: {len(pairs)}")
for p in pairs:
    print(f"      {p['o']/1e6:.4f} <- {p['i']/1e6:.4f}  "
          f"conf {p['confidence']}  lag {p['median_lag_s']*1000:.0f} ms")
got = {(p["o"], p["i"]) for p in pairs}
if (462_675_000, 467_675_000) not in got:
    bad.append("did not find the 462.675/467.675 repeater")
if (462_700_000, 467_700_000) in got:
    bad.append("paired 462.700/467.700 — correct offset but uncorrelated timing")

# Contested tones must not be promoted; unchecked tones must not be either.
for f, want_state, want_max in ((462_562_500, "unknown", 2),
                                (467_850_000, "unknown", 2),
                                (462_700_000, "none",    4)):
    r = db.execute("SELECT tone_state, tier, label FROM channels WHERE freq_hz=?",
                   (f,)).fetchone()
    if r is None:
        bad.append(f"{f/1e6:.4f} has no channel row"); continue
    print(f"  {f/1e6:9.4f}  tone_state={r['tone_state']:8} tier={r['tier']}")
    if r["tone_state"] != want_state:
        bad.append(f"{f/1e6:.4f} tone_state {r['tone_state']!r}, expected {want_state!r}")
    if r["tier"] > want_max:
        bad.append(f"{f/1e6:.4f} tier {r['tier']} exceeds {want_max}")

# Bandwidth, not power, must decide FRS vs GMRS on shared channels.
import statistics
for f, want in ((462_675_000, "gmrs-only"), (462_650_000, "shared")):
    c = db.execute("SELECT * FROM channels WHERE freq_hz=?", (f,)).fetchone()
    if c is None:
        bad.append(f"{f/1e6:.4f} has no channel row"); continue
    evs = db.execute("SELECT bandwidth_hz, snr_db FROM events WHERE channel_id=?",
                     (c["id"],)).fetchall()
    bw = statistics.median(e["bandwidth_hz"] for e in evs)
    snr = statistics.median(e["snr_db"] for e in evs)
    shared = "/" in (c["label"] or "")
    print(f"  {f/1e6:9.4f}  {c['label']:<20} {bw/1000:5.1f} kHz  {snr:5.1f} dB SNR")
    if want == "gmrs-only" and shared:
        bad.append(f"{f/1e6:.4f} is {bw/1000:.1f} kHz — too wide for FRS, should be GMRS alone")
    if want == "shared" and not shared:
        bad.append(f"{f/1e6:.4f} is narrowband at {bw/1000:.1f} kHz — must stay shared "
                   f"(loudest channel in the set; a power rule would mislabel it)")

# The whole chain must be reproducible.
runs = [subprocess.run([sys.executable, "src/enrich.py", "data/survey.sqlite",
                        "--profile", "profiles/festival.yaml"],
                       capture_output=True, text=True).stdout for _ in range(2)]
print(f"  deterministic across runs: {runs[0] == runs[1]}")
if runs[0] != runs[1]: bad.append("enrichment is not deterministic")

if bad:
    print("\nFAILED:")
    for b in bad: print(f"  - {b}")
    sys.exit(1)
print("\n  all checks passed")
PY

echo
echo "== 8. committing =="
git add -A
if git diff --cached --quiet; then
    echo "  nothing to commit — already applied"
else
    git commit -q -m "Enrichment pipeline and schema v4

Adds src/enrich.py: tags events against the band plan, rolls them into channels,
infers repeater pairs from correlated keyups, and scores contactability 0-4.
Each pass is idempotent; channels and pairs are fully rebuildable, except
channels.notes which is hand-written and preserved.

Adds src/migrate.py so schema changes apply to an existing database in place
rather than requiring a rebuild and a re-paste of schema.sql.

  v3  events.tone_state and channels.tone_state. 'no tone present' and 'never
      checked' were both NULL and are very different facts — the first means you
      can programme a radio and be heard, the second means you cannot.
  v4  rebuilds v_events and v_contactable, which rendered both as a blank column.

Adds tools/make_fixtures.py: a deterministic synthetic festival scenario used to
develop all of the above with no radio attached. Includes the awkward cases — a
shared channel with three competing tones, a channel never tone-checked, an
unallocated frequency, and a decoy at a valid 5 MHz offset with uncorrelated
timing that must not be reported as a repeater.

Tone agreement threshold is 0.8, not 0.6: at 0.6 a shared FRS channel where a
third of traffic runs CTCSS was recorded as 'no tone' and promoted to tier 4.

Adds operator.licences to the profile. Tier 4 means you could legally join in,
which depends on what you hold, not on the signal.

Distinguishes FRS from GMRS on shared channels by occupied bandwidth. FRS is
capped at 12.5 kHz; GMRS main channels 15-22 are allowed 20 kHz, so anything
measurably wider than 12.5 kHz there cannot be FRS. The inference is one-sided:
wide rules FRS out, narrow rules nothing out, because narrowband GMRS radios are
common and indistinguishable from FRS.

Received power was considered and rejected. It is transmit power minus path loss,
and path loss varies by tens of dB across a site, so a close FRS handheld reads
louder than a distant GMRS repeater. Bandwidth is a property of the transmission
rather than the path and survives an unknown distance. The fixtures include a
control case for this: 462.650 is the loudest channel in the set at 56 dB SNR and
correctly stays FRS | GMRS because it is narrowband.

Corrects GMRS main channel bandwidth to 20 kHz, the authorised emission bandwidth
under 95.1771. It was seeded as 25 kHz, which is the channel spacing."
    echo "  committed"
fi

echo
git --no-pager log --oneline -1
echo
echo "Push when ready:  git push"
