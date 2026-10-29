#!/usr/bin/env bash

# run_all.sh - every test of the fixed-work benchmark, one after another
#
# Usage: bash scripts/bench/tests/run_all.sh [--quick]
#   --quick   leave out the Nextflow and whole-session tests (they need Java 17+
#             and Nextflow 26.04.4 and take about 20 minutes)
# Environment: NEXTFLOW (path to nextflow), NXF_HOME, PYTHON (default python3).
# Runs on Linux or WSL. Prints one line per test file and the end of the log of
# every failing one. Exit 0 only if every test passed.

set -u
export LC_ALL=C
TESTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
QUICK=0
[[ "${1:-}" == --quick ]] && QUICK=1
LOGS="$(mktemp -d /tmp/bench_tests.XXXXXX)"
FAILED=()

run() {   # NAME COMMAND...
    local name="$1" t0=$SECONDS
    shift
    printf '%-18s ' "$name"
    if "$@" > "$LOGS/$name.log" 2>&1 < /dev/null; then
        printf 'ok      (%s s)  %s\n' "$((SECONDS - t0))" "$(grep -E '^(Ran |passed:)' "$LOGS/$name.log" | tail -n 1)"
    else
        printf 'FAILED  (%s s)\n' "$((SECONDS - t0))"
        tail -n 25 "$LOGS/$name.log" | sed 's/^/      /'
        FAILED+=("$name")
    fi
}

for t in test_benchlib test_cases test_session_py test_check test_calibrate; do
    run "$t" "$PY" -I "$TESTS/$t.py"
done
run test_run_case bash "$TESTS/test_run_case.sh"
run test_setup bash "$TESTS/test_setup.sh"
if (( QUICK )); then
    echo "(--quick: test_nextflow and test_session left out)"
elif [[ -z "${NEXTFLOW:-}" && -z "$(command -v nextflow)" ]]; then
    echo "test_nextflow and test_session left out: no Nextflow (set NEXTFLOW=/path/to/nextflow)"
    FAILED+=("no-nextflow")
else
    run test_nextflow bash "$TESTS/test_nextflow.sh"
    run test_session bash "$TESTS/test_session.sh"
fi

echo
if (( ${#FAILED[@]} )); then
    echo "NOT ALL PASSED: ${FAILED[*]} (logs in $LOGS)"
    exit 1
fi
rm -rf "$LOGS"
echo "ALL PASSED"
exit 0
