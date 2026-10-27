#!/bin/bash
# run_pilot_smoke.sh - runs pilot_a.sh --smoke with FAKE tools, to check that the script itself runs through,
# writes scan.csv with the expected columns and parses the tool output. It checks nothing about the machine.
# Usage: bash scripts/tests/run_pilot_smoke.sh [work_dir]
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(cd "$HERE/.." && pwd)"
T="${1:-$(mktemp -d)}"; mkdir -p "$T"; T="$(cd "$T" && pwd)"
source "$HERE/lib_stubs.sh"
make_stubs "$T"
export PILOT_SUDO=""          # no sudo in the test
export RAPL_LOCK_FILE="$T/lock"
cd "$T" || exit 1
bash "$SCRIPTS/pilot_a.sh" --smoke "$T/pilot_out" > "$T/pilot_stdout.txt" 2>&1
rc=$?
PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  ok   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }
[[ $rc -eq 0 ]] && ok "pilot_a.sh --smoke exits 0" || bad "pilot_a.sh exit code $rc"
[[ -s "$T/pilot_out.tar.gz" ]] && ok "archive written" || bad "archive missing"
S="$T/pilot_out/scan.csv"
[[ -s "$S" ]] && ok "scan.csv written" || bad "scan.csv missing"
n=$(awk 'NR > 1' "$S" | wc -l | tr -d ' ')
[[ "$n" -ge 10 ]] && ok "scan.csv has $n rows" || bad "scan.csv has only $n rows"
awk -F, 'NR == 1 { n = NF } NR > 1 && NF != n { bad = 1 } END { exit bad }' "$S" && ok "scan.csv: every row has the header's field count" || bad "scan.csv field count"
awk -F, 'NR == 1 { for (i = 1; i <= NF; i++) c[$i] = i } NR > 1 && $(c["name"]) == "ops_semantics_cpu8" { print $(c["ops"]), $(c["instructions"]), $(c["dram_read_mib"]) }' "$S" | grep -q "^20000 \|^2000 " && ok "ops parsed from the stress-ng log" || bad "ops not parsed"
awk -F, 'NR == 1 { for (i = 1; i <= NF; i++) c[$i] = i } NR > 1 && $(c["kind"]) == "io" { print $(c["ops"]) }' "$S" | grep -q "^[0-9][0-9]*$" && ok "fio bytes parsed" || bad "fio bytes not parsed"
awk -F, 'NR == 1 { for (i = 1; i <= NF; i++) c[$i] = i } NR > 1 && $(c["name"]) ~ /^cpu_all/ { print $(c["instructions"]) }' "$S" | grep -q "^1000000000$" && ok "perf values parsed" || bad "perf values not parsed"
[[ -s "$T/pilot_out/check_rapl_permissions.txt" ]] && ok "RAPL permission check file written" || bad "check_rapl_permissions.txt missing"
grep -q "^PASS rapl_package_readable" "$T/pilot_out/checks.txt" && ok "check rapl_package_readable passes with the fake tree" || bad "rapl_package_readable check"
grep -q "^PASS perf_four_events" "$T/pilot_out/checks.txt" && ok "check perf_four_events passes with the fake tools" || bad "perf check"
grep -q "^PASS fio_io_size_end_fsync" "$T/pilot_out/checks.txt" && ok "check fio_io_size_end_fsync passes" || bad "fio check"
grep -q "^PASS turbostat_columns" "$T/pilot_out/checks.txt" && ok "check turbostat_columns passes" || bad "turbostat check"
[[ "$(awk 'END { print NR }' "$T/pilot_out/hot_probe.csv")" -ge 3 ]] && ok "hot_probe.csv sampled" || bad "hot_probe.csv too short"
echo; echo "passed: $PASS   failed: $FAIL"
[[ $FAIL -eq 0 ]]
