#!/usr/bin/env bash

# test_run_case.sh - end-to-end test of run_case.sh, window.sh and sampler.sh
#
# Uses stand-in tools (stress-ng, fio, perf, taskset) that print the real
# output formats, and a fake sysfs/proc/cgroup tree whose RAPL counters a
# background "ticker" raises (fake_machine.sh). Every run is started
# the way the session will start it: no terminal (setsid), empty environment
# (env -i, so no PATH), stdin /dev/null.
#
# Real RAPL, perf, hwmon, cgroups, governor writes, stress-ng and fio are NOT
# used here; the pilot checks them on the laptop.
#
# Usage: bash scripts/bench/tests/test_run_case.sh   (any Linux; used: WSL1 Ubuntu 22.04)

set -u
export LC_ALL=C
BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=fake_machine.sh
source "$BENCH/tests/fake_machine.sh"
T="$(mktemp -d /tmp/bench_test.XXXXXX)"
PASS=0
FAIL=0

finish_all() {
    fake_machine_stop
    rm -rf "$T"
}
trap finish_all EXIT

ok()  { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

fake_machine_start "$T"       # sets R, CTL, BIN, F_* paths, writes $T/session.env, starts the ticker
mkdir -p "$T/specs" "$T/runs"

# ---- specs, runner, checks ------------------------------------------------------------

spec() {   # NAME MODE LOGGING EXPECTED_S PARTS_JSON
    cat > "$T/specs/$1.json" <<EOF
{"run_id": "$1", "case_id": "test_$1", "mode": "$2", "logging": $3, "governor": "powersave",
 "epp": "balance_performance", "expected_s": $4, "round": 1, "parts": $5}
EOF
}

# run NAME [ATTEMPT MAX] - start run_case.sh like the session will; sets RC, DIR, SECS
run() {
    local name="$1" attempt="${2:-1}" max="${3:-3}" t0
    DIR="$T/runs/$name/a$attempt"
    mkdir -p "$T/runs/$name"
    t0=$SECONDS
    setsid -w env -i /bin/bash "$BENCH/run_case.sh" "$T/session.env" "$T/specs/$name.json" "$DIR" "$attempt" "$max" \
        < /dev/null > "$T/runs/$name/stdout_a$attempt.txt" 2>&1
    RC=$?
    SECS=$((SECONDS - t0))
}

verdict_is() { [[ "$(head -n 1 "$DIR/verdict.txt" 2>/dev/null)" == "$1" ]]; }
hard_has() { grep -q "^hard: .*$1" "$DIR/verdict.txt" 2>/dev/null; }
soft_has() { grep -q "^soft: .*$1" "$DIR/verdict.txt" 2>/dev/null; }
col() { python3 -c 'import csv,sys; print(next(csv.DictReader(open(sys.argv[1])))[sys.argv[2]])' "$DIR/result.csv" "$1" 2>/dev/null; }
col_is() { [[ "$(col "$1")" == "$2" ]]; }
col_set() { [[ -n "$(col "$1")" ]]; }
col_between() { python3 -c 'import sys; v=float(sys.argv[1]); sys.exit(0 if float(sys.argv[2]) <= v <= float(sys.argv[3]) else 1)' "$(col "$1")" "$2" "$3" 2>/dev/null; }
rc_is() { [[ "$RC" == "$1" ]]; }
no_leftovers() { ! pgrep -f "$BIN/" > /dev/null && ! pgrep -f "$BENCH/sampler.sh" > /dev/null; }
has_line() { grep -q -e "$1" "$2" 2>/dev/null; }
count_lines() { [[ "$(grep -c -e "$1" "$2" 2>/dev/null)" == "$3" ]]; }
wrapped_inside() { awk '/^win_start/ { s = $3 } /^win_end/ { e = $3 } END { exit !(e < s) }' "$DIR/window.txt"; }
valid_marker() { [[ "$(cat "$T/runs/$1/VALID" 2>/dev/null)" == "$2" ]]; }
no_marker() { [[ ! -e "$T/runs/$1/VALID" ]]; }
show_on_fail() { (( FAIL > ${1:-0} )) && { echo "    --- run_case.log:"; sed 's/^/    /' "$DIR/run_case.log" 2>/dev/null | tail -n 15; cat "$DIR/verdict.txt" 2>/dev/null | sed 's/^/    /'; }; return 0; }

CPU='{"name": "cpu", "kind": "cpu", "threads": 8, "ops": 1000}'
VM='{"name": "ram", "kind": "vm", "vm_bytes": "1G", "ops": 50}'
IO='{"name": "io", "kind": "io", "pattern": "write", "bytes": 1048576}'

make_env < /dev/null

# ---- scenarios -----------------------------------------------------------------------

echo "1. parallel CPU + memory, logging on"
f0=$FAIL
spec par_cpu_vm parallel true 1 "[$CPU, $VM]"
run par_cpu_vm
check "exit 0" rc_is 0
check "verdict VALID" verdict_is VALID
check "VALID marker names the attempt" valid_marker par_cpu_vm a1
check "package power plausible ($(col pkg_power_w) W, stand-in ticks 25 W at most)" col_between pkg_power_w 2 26
check "DRAM energy present" col_set dram_energy_j
check "CPU ops done = target" col_is cpu_ops_done 1000
check "memory ops done = target" col_is vm_ops_done 50
check "perf totals read" col_set instructions
check "clock computed" col_set busy_mhz
check "perf ran with -I and --summary" has_line "-I 1000 --summary" "$CTL/perf_calls"
check "sampler wrote lines" test -s "$DIR/sampler.txt"
check "window start and end recorded" count_lines '^win_' "$DIR/window.txt" 2
check "two part rows" count_lines '^par_cpu_vm,' "$DIR/parts.csv" 2
check "temperature at start" col_is temp_start_c 50
check "RAPL limits read" col_is mmio_pl1_w_start 15
check "other programs' CPU measured" col_set other_cpu_per_s
check "no processes left" no_leftovers
show_on_fail "$f0"

echo "2. sequential CPU, memory, I/O"
f0=$FAIL
spec seq_cpu_vm_io sequential true 3 "[$CPU, $VM, $IO]"
run seq_cpu_vm_io
check "exit 0" rc_is 0
check "three sub-windows" count_lines '^sub_start' "$DIR/window.txt" 3
check "I/O bytes done = target" col_is io_bytes_done 1048576
check "sub-window energy per part" python3 -c 'import csv,sys; r=list(csv.DictReader(open(sys.argv[1]))); sys.exit(0 if all(x["sub_pkg_energy_j"] for x in r) else 1)' "$DIR/parts.csv"
check "fio test file deleted" test -z "$(ls "$T/io")"
check "fio file created before the window" test ! -s "$CTL/violations"
check "no processes left" no_leftovers
show_on_fail "$f0"

echo "3. idle, logging off"
f0=$FAIL
n_perf=$(wc -l < "$CTL/perf_calls")
spec idle_nolog parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run idle_nolog
check "exit 0" rc_is 0
check "no perf output" test ! -e "$DIR/perf.csv"
check "perf not started" test "$(wc -l < "$CTL/perf_calls")" -eq "$n_perf"
check "no sampler" test ! -e "$DIR/sampler.txt"
show_on_fail "$f0"

echo "4. fixed time, pinned (mechanism block)"
f0=$FAIL
spec pinned parallel true 1 '[{"kind": "cpu", "threads": 3, "fixed_s": 1, "pin": "0-2"}, {"kind": "vm", "vm_bytes": "1G", "fixed_s": 1, "pin": "3"}]'
run pinned
check "exit 0" rc_is 0
check "taskset -c 0-2" has_line "^-c 0-2 " "$CTL/taskset_calls"
check "taskset -c 3" has_line "^-c 3 " "$CTL/taskset_calls"
check "fixed time passed to stress-ng" has_line "--timeout 1s" "$CTL/stress_calls"
show_on_fail "$f0"

echo "5. work not complete"
f0=$FAIL
echo short > "$CTL/stress_mode"
spec short parallel true 1 "[$CPU]"
run short
rm -f "$CTL/stress_mode"
check "exit 3" rc_is 3
check "verdict RETRY" verdict_is RETRY
check "reason work_incomplete" hard_has work_incomplete_p0
check "no VALID marker" no_marker short
check "result.csv still written (valid=0)" col_is valid 0
show_on_fail "$f0"

echo "6. hanging tool, watchdog"
f0=$FAIL
echo hang > "$CTL/stress_mode"
spec hang parallel true 1 "[$CPU, $VM]"
run hang
rm -f "$CTL/stress_mode"
check "exit 3" rc_is 3
check "reason watchdog_fired" hard_has watchdog_fired
check "stopped within 30 s (took ${SECS} s)" test "$SECS" -lt 30
check "no processes left" no_leftovers
show_on_fail "$f0"

echo "7. another window holds the lock"
f0=$FAIL
flock "$T/window.lock" sleep 3 &     # (killing flock would not free the lock: its sleep keeps it)
holder=$!
sleep 0.5
spec locked parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run locked
wait "$holder"
check "exit 3" rc_is 3
check "reason lock_busy" hard_has lock_busy
check "no window_incomplete on top" bash -c "! grep -q window_incomplete '$DIR/verdict.txt'"
show_on_fail "$f0"

echo "8. governor cannot be set"
f0=$FAIL
rm "$F_CPU/cpu1/cpufreq/scaling_governor"
mkdir "$F_CPU/cpu1/cpufreq/scaling_governor"      # unreadable and unwritable, also for root
spec gov parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run gov
rmdir "$F_CPU/cpu1/cpufreq/scaling_governor"; put "$F_CPU/cpu1/cpufreq/scaling_governor" powersave
check "exit 3" rc_is 3
check "reason governor_not_as_planned" hard_has governor_not_as_planned
check "flag governor_set_failed" soft_has governor_set_failed
show_on_fail "$f0"

echo "9. power cable out"
f0=$FAIL
put "$F_AC" 0
spec ac parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run ac
put "$F_AC" 1
check "exit 3" rc_is 3
check "reason ac_timeout" hard_has ac_timeout
check "gave up after the wait limit (${SECS} s)" test "$SECS" -lt 15
show_on_fail "$f0"

echo "10. other programs busy: retry, then kept on the last attempt"
f0=$FAIL
put "$CTL/system_rate" 50000                        # half a CPU
spec busy parallel false 1 '[{"kind": "idle", "seconds": 2}]'
run busy 1 3
check "attempt 1: exit 3" rc_is 3
check "attempt 1: reason disturbed" hard_has disturbed
run busy 3 3
check "attempt 3: exit 0" rc_is 0
check "attempt 3: flag disturbed_kept_last_attempt" soft_has disturbed_kept_last_attempt
check "VALID marker names attempt 3" valid_marker busy a3
put "$CTL/system_rate" 1000
show_on_fail "$f0"

echo "11. RAPL counter wraps during the window"
f0=$FAIL
read -r top < "$F_PKG/max_energy_range_uj"
put "$F_PKG/energy_uj" $((top - 50000000))     # wraps about 2 s from now, inside the 5 s window
spec wrap parallel false 5 '[{"kind": "idle", "seconds": 5}]'
run wrap
check "exit 0" rc_is 0
check "counter wrapped inside the window" wrapped_inside
check "power after wrap plausible ($(col pkg_power_w) W)" col_between pkg_power_w 2 26
show_on_fail "$f0"

echo "12. perf cannot start"
f0=$FAIL
echo broken > "$CTL/perf_mode"
spec noperf parallel true 1 "[$CPU]"
run noperf
rm -f "$CTL/perf_mode"
check "exit 0 (window ran without perf)" rc_is 0
check "flag perf_fallback" soft_has perf_fallback
check "flag perf_missing" soft_has perf_missing
show_on_fail "$f0"

echo "13. stress-ng counts --cpu-ops per worker"
f0=$FAIL
echo per_instance > "$CTL/ops_semantics"
make_env <<< "OPS_SEMANTICS='per_instance'"
spec perinst parallel false 1 '[{"kind": "cpu", "threads": 4, "ops": 1001}]'
run perinst
rm -f "$CTL/ops_semantics"
make_env < /dev/null
check "exit 0" rc_is 0
check "flag value per worker" has_line "--cpu-ops 251 " "$CTL/stress_calls"
check "target = 4 x 251" col_is cpu_ops_target 1004
show_on_fail "$f0"

echo "14. temperature not readable / still hot"
f0=$FAIL
make_env <<< "TEMP_FILE='$T/no_such_file'"
spec notemp parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run notemp
check "no temperature: exit 0" rc_is 0
check "no temperature: flag temp_unreadable" soft_has temp_unreadable
make_env < /dev/null
put "$F_TEMP" 80000
spec hot parallel false 1 '[{"kind": "idle", "seconds": 1}]'
run hot
put "$F_TEMP" 50000
check "still hot: exit 0" rc_is 0
check "still hot: flag settle_timeout" soft_has settle_timeout
check "still hot: waited the maximum" col_between settle_s 3 4
show_on_fail "$f0"

echo "15. not enough free disk for the fio file"
f0=$FAIL
make_env <<< "DISK_KEEP_FREE_KB='999999999999'"      # far more than any disk has
n_fio=$(wc -l < "$CTL/fio_calls")
spec lowdisk parallel false 1 "[$IO]"
run lowdisk
make_env < /dev/null
check "exit 3" rc_is 3
check "reason disk_low" hard_has disk_low
check "fio never started (no file created)" test "$(wc -l < "$CTL/fio_calls")" -eq "$n_fio"
check "nothing left in the fio folder" test -z "$(ls "$T/io")"
show_on_fail "$f0"

echo "16. nothing read stdin or had a terminal"
check "no violations recorded" test ! -s "$CTL/violations"
[[ -s "$CTL/violations" ]] && sed 's/^/    /' "$CTL/violations"

echo
echo "passed: $PASS   failed: $FAIL"
(( FAIL == 0 ))
