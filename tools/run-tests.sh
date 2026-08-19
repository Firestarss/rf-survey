#!/usr/bin/env bash
# The whole test suite. Stdlib unittest, no third-party dependencies, because
# this has to run on the deck itself.
#
#   bash tools/run-tests.sh            # everything
#   bash tools/run-tests.sh -v         # per-test names
#   bash tools/run-tests.sh test_dcs   # one module
#
# The end-to-end test drives the real capture loop against a synthetic radio and
# takes a while on a Pi. RFSURVEY_SKIP_SLOW=1 leaves it out.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -gt 0 ] && [[ "${1:-}" != -* ]]; then
    mod="$1"; shift
    exec python3 -m unittest discover -s tests -t tests -p "${mod%.py}.py" "$@"
fi
exec python3 -m unittest discover -s tests -t tests "$@"
