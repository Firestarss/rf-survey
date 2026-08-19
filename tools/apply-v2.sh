#!/usr/bin/env bash
# Apply schema v2 and the band plan rebuild.
#
# Run from ~/rfsurvey AFTER scp-ing the four updated files into place:
#   src/schema.sql  src/bandplan.py  tools/seed_band_plan.py  profiles/festival.yaml
#
#   bash tools/apply-v2.sh
#
# Verifies before it commits. If any check fails, nothing is committed.

set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok   $*"; }

echo "== 1. checking files are present =="
for f in src/schema.sql src/bandplan.py src/db.py tools/seed_band_plan.py \
         profiles/festival.yaml; do
    [ -s "$f" ] || fail "$f missing or empty — scp it over first"
    ok "$f"
done

echo
echo "== 2. syntax =="
python3 -m py_compile src/db.py src/bandplan.py tools/seed_band_plan.py \
    || fail "python syntax error"
ok "python parses"
python3 -c "import yaml,sys; yaml.safe_load(open('profiles/festival.yaml'))" \
    || fail "festival.yaml is not valid YAML"
ok "yaml parses"

echo
echo "== 3. bumping SCHEMA_VERSION in db.py =="
if grep -q '^SCHEMA_VERSION = 2$' src/db.py; then
    ok "already 2"
else
    sed -i 's/^SCHEMA_VERSION = 1$/SCHEMA_VERSION = 2/' src/db.py
    grep -q '^SCHEMA_VERSION = 2$' src/db.py || fail "sed did not take — edit by hand"
    ok "1 -> 2"
fi

echo
echo "== 4. rebuilding database =="
# data/ is gitignored and holds nothing yet. Once there are real survey rows this
# becomes a migration, not a delete.
rm -f data/survey.sqlite data/survey.sqlite-wal data/survey.sqlite-shm
python3 src/db.py data/survey.sqlite
[ "$(head -c 15 data/survey.sqlite)" = "SQLite format 3" ] \
    || fail "not a SQLite file"
ok "header is a real database"

echo
echo "== 5. seeding band plan =="
python3 tools/seed_band_plan.py data/survey.sqlite

echo
echo "== 6. verifying =="
python3 - <<'PY' || exit 1
import sqlite3, sys, yaml
db = sqlite3.connect("data/survey.sqlite"); db.row_factory = sqlite3.Row
bad = []

n = db.execute("SELECT COUNT(*) FROM band_plan").fetchone()[0]
print(f"  band_plan rows: {n}")
if n < 110: bad.append(f"expected ~117 rows, got {n}")

v = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
print(f"  schema version: {v}")
if v != "2": bad.append(f"schema says v{v}")

ov = db.execute("""SELECT COUNT(*) FROM band_plan a JOIN band_plan b
                   ON a.service=b.service AND a.id<b.id
                   AND a.kind='channel' AND b.kind='channel'
                   AND a.freq_lo_hz<=b.freq_hi_hz AND b.freq_lo_hz<=a.freq_hi_hz"""
              ).fetchone()[0]
print(f"  overlapping windows: {ov}")
if ov: bad.append(f"{ov} overlapping channel windows")

sys.path.insert(0, "src")
import bandplan
for f, want in ((146_520_000, "national FM simplex calling"),
                (146_519_400, "national FM simplex calling"),
                (462_674_800, "GMRS 20"),
                (158_400_000, "158.400")):
    got = bandplan.describe(db, f)
    print(f"  {got}")
    if want not in got: bad.append(f"{f/1e6:.4f} did not resolve to {want!r}")

# every VHF channel of interest must fall inside a rotation window
prof = yaml.safe_load(open("profiles/festival.yaml"))
wins = [w["center_hz"] for w in prof["receivers"]["vhf"]["windows"]]
wins.append(prof["receivers"]["uhf"]["center_hz"])
HALF = 4_250_000  # conservative usable bandwidth, not the full 10 MSPS
rows = db.execute("""SELECT freq_center_hz f, label FROM band_plan
                     WHERE kind='channel' AND service IN ('murs','part90','frs','gmrs')"""
                 ).fetchall()
miss = [r for r in rows if not any(c-HALF <= r["f"] <= c+HALF for c in wins)]
print(f"  channels outside every window (at +/-4.25 MHz): {len(miss)}")
for m in miss:
    print(f"      {m['f']/1e6:9.4f}  {m['label']}")
if any("158.400" in m["label"] for m in miss):
    bad.append("158.400 still not covered — did festival.yaml update?")

if bad:
    print("\nFAILED:")
    for b in bad: print(f"  - {b}")
    sys.exit(1)
print("\n  all checks passed")
PY

echo
echo "== 7. committing =="
git add -A
if git diff --cached --quiet; then
    echo "  nothing to commit — already applied"
else
    git commit -q -m "Schema v2: band plan as ranges; VHF window 153.2 -> 154.95 MHz

band_plan now stores frequency ranges rather than single frequencies. A discrete
channel is a narrow range, a ham band segment a wide one, and one containment
query handles both with the narrowest match winning. Match tolerance moves out of
enricher code and into the data.

Adds ARRL 2 m and 70 cm segments (placeholders pending NESMC), a bandplan lookup
module, and band_plan_id links on events and channels.

Moves the VHF rotation centre to 154.950 MHz, the midpoint of the 151.505-158.400
span. Covers all 14 VHF business and MURS channels even at a conservative 8.5 MHz
usable bandwidth; 153.200 missed the 158.400 itinerant by 200 kHz."
    echo "  committed"
fi

echo
git --no-pager log --oneline -1
echo
echo "Push when ready:  git push"
