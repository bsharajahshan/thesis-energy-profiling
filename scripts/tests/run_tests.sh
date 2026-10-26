#!/bin/bash
# run_tests.sh - tests for measure_rapl.sh, run_case.sh and the parsers with FAKE stress-ng / fio / perf /
# flock / taskset programs and a fake RAPL tree. Runs on any machine with bash and awk (also on Windows Git Bash).
# Real-tool formats are checked against the logs of 2026-10-15 when OCT15_RAW is given:
#   OCT15_RAW=<.../01_diagnostics/full_run_60s_5reps/raw> bash scripts/tests/run_tests.sh
# Usage: bash scripts/tests/run_tests.sh [work_dir]

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(cd "$HERE/.." && pwd)"
T="${1:-$(mktemp -d)}"
mkdir -p "$T"; T="$(cd "$T" && pwd)"
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); echo "  ok   $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL $1  (got '$2', wanted '$3')"; }
eq()   { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1" "$2" "$3"; fi; }

# ---- fake RAPL tree and fake tools (shared with run_pilot_smoke.sh) ------------------------------------------
source "$HERE/lib_stubs.sh"
make_stubs "$T"
mkdir -p "$T/work"

cat > "$T/calibration.env" <<'EOF'
CPU_METHOD=matrixprod
CPU_OPS_TOTAL=100000
CPU_OPS_SEMANTICS=total
VM_METHOD=write64
VM_TARGET_GIB=300
VM_OPS_PER_GIB_C1=7000
VM_OPS_PER_GIB_C2=6000
VM_OPS_PER_GIB_C4=5000
VM_OPS_PER_GIB_C8=4000
IO_TOTAL_GIB=8
WATCHDOG_S=600
PERF_ON=0
PERF_EVENTS="instructions,cycles,uncore_imc/data_reads/,uncore_imc/data_writes/"
EOF
export CALIBRATION="$T/calibration.env" RAPL_LOCK_FILE="$T/lock"
cd "$T/work" || exit 1

# column access by header name
col() { # col <csv> <row_number(1=first data row)> <name>
    awk -F, -v r="$2" -v n="$3" 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == n) c = i } NR == r + 1 { print $c }' "$1"
}
rows() { awk 'NR > 1' "$1" | wc -l | tr -d ' '; }
consistent() { # every row has as many fields as the header
    awk -F, 'NR == 1 { n = NF } NR > 1 && NF != n { bad = 1 } END { exit bad }' "$1" && ok "$2: all rows have the header's number of fields" || bad "$2: field count" "mismatch" "equal"
}

echo "== parsers on the real logs of 2026-10-15 =="
source "$SCRIPTS/shlib/parsers.sh"
if [[ -n "${OCT15_RAW:-}" && -d "${OCT15_RAW}" ]]; then
    eq "stress_metrics cpu (real log)" "$(stress_metrics "$OCT15_RAW/cpu_ram_1/cpu8.log" cpu)" "359270,60.00,401.73,0.28" 2>/dev/null || true
    r="$(stress_metrics "$OCT15_RAW/ram_io_1/vm.log" vm)"; eq "stress_metrics vm (real log) ops" "${r%%,*}" "2457600"
    r="$(fio_metrics "$OCT15_RAW/ram_io_1/io.json")"; IFS=, read -r b i t <<< "$r"
    [[ "$b" =~ ^[0-9]+$ && "$b" -gt 1000000000 ]] && ok "fio_metrics (real json) bytes=$b iops=$i runtime_ms=$t" || bad "fio_metrics real" "$r" "bytes>1e9"
else
    echo "  (skipped: set OCT15_RAW to check the real log formats)"
fi
printf '%s\n' "stress-ng: metrc: [1] cpu 359270 60.00 401.73 0.28 5987.57 893.69" "stress-ng: info: [1] passed: 8: cpu (8)" > "$T/m.log"
eq "stress_metrics cpu (sample line)" "$(stress_metrics "$T/m.log" cpu)" "359270,60.00,401.73,0.28"
eq "stress_metrics missing stressor" "$(stress_metrics "$T/m.log" vm)" ",,,"
eq "stress_metrics missing file" "$(stress_metrics "$T/nofile" cpu)" ",,,"
printf '%s\n' "1048873819574,,instructions,1,100,," "799761971434,,cycles,1,100,," "124234.92,MiB,uncore_imc/data_reads/,1,100,," "68014.70,MiB,uncore_imc/data_writes/,1,100,," > "$T/p.csv"
eq "perf_metrics" "$(perf_metrics "$T/p.csv")" "1048873819574,799761971434,124234.92,68014.70"
eq "comp_get" "$(comp_get "cpu:threads=4,div=2,cpus=0-3" div 1)" "2"
eq "comp_get default" "$(comp_get "io:rw=write" div 1)" "1"
eq "comp_get no colon" "$(comp_get "idle" secs 60)" "60"

echo "== measure_rapl.sh =="
out="$(bash "$SCRIPTS/measure_rapl.sh" "stress-ng --cpu 1 --timeout 1s" t1 2>/dev/null)"
eq "energy = fake +2 J" "$(kv "$out" ENERGY_J)" "2.000000"
eq "dram = fake +0.1 J" "$(kv "$out" DRAM_ENERGY_J)" "0.100000"
[[ "$(kv "$out" END_NS)" -gt "$(kv "$out" START_NS)" ]] && ok "END_NS > START_NS" || bad "start/end" "$(kv "$out" END_NS)" ">"
echo 999999000 > "$RAPL_ROOT/intel-rapl:0/energy_uj"
echo 262143328850 > "$RAPL_ROOT/intel-rapl:0/max_energy_range_uj"
WORKLOAD_LOG="$T/wl.log" bash "$SCRIPTS/measure_rapl.sh" "stress-ng --cpu 1 --timeout 1s" t2 >/dev/null 2>&1
[[ -s "$T/wl.log" ]] && ok "WORKLOAD_LOG receives the workload output" || bad "WORKLOAD_LOG" "empty" "text"
# wraparound: counter after < before
echo 262143328000 > "$RAPL_ROOT/intel-rapl:0/energy_uj"
out="$(bash "$SCRIPTS/measure_rapl.sh" "p=$RAPL_ROOT/intel-rapl:0/energy_uj; echo 1000000 > \$p" t3 2>/dev/null)"
eq "wraparound delta (uJ)" "$(kv "$out" ENERGY_UJ)" "$((262143328850 - 262143328000 + 1000000))"

echo "== run_case.sh =="
run() { bash "$SCRIPTS/run_case.sh" "$@" 2>/dev/null; }

: > "$STUB_LOG"
run "$T/o1" c_cpu8 rq1_cpu single cpu 8 1 100 1 7 -- "cpu:threads=8,div=1" > /dev/null
f="$T/o1/c_cpu8.csv"
eq "single cpu: 1 row" "$(rows "$f")" "1"; consistent "$f" "single cpu"
eq "cpu_ops = requested" "$(col "$f" 1 cpu_ops)" "100000"
eq "cpu_ops_req" "$(col "$f" 1 cpu_ops_req)" "100000"
eq "work_complete" "$(col "$f" 1 work_complete)" "1"
eq "energy_j (fake)" "$(col "$f" 1 energy_j)" "2.000000"
eq "run_index" "$(col "$f" 1 run_index)" "7"
eq "pl_long_w" "$(col "$f" 1 pl_long_w)" "200.00"
grep -q -- "--cpu 8 --cpu-method matrixprod --cpu-ops 100000" "$STUB_LOG" && ok "stress-ng command line (cpu)" || bad "cpu command" "$(grep stress-ng "$STUB_LOG" | head -1)" "--cpu 8 ... --cpu-ops 100000"

: > "$STUB_LOG"
run "$T/o2" c_par rq2 parallel cpu_ram_io max 3 100 2 5 -- "cpu:threads=8,div=1" "ram:chunk_gb=8,div=1" "io:rw=write" > /dev/null
f="$T/o2/c_par.csv"
eq "parallel: 1 window" "$(rows "$f")" "1"; consistent "$f" "parallel"
eq "parallel window_name" "$(col "$f" 1 window_name)" "cpu_ram_io"
eq "vm_ops_req = 300 GiB x 4000" "$(col "$f" 1 vm_ops_req)" "1200000"
eq "vm_gib_est = 300" "$(col "$f" 1 vm_gib_est)" "300.000"
eq "io_bytes = 8 GiB" "$(col "$f" 1 io_bytes)" "8589934592"
eq "work_complete (parallel)" "$(col "$f" 1 work_complete)" "1"
grep -q -- "--create_only=1" "$STUB_LOG" && ok "fio file created before the window" || bad "fio create_only" "missing" "present"
grep -q -- "--io_size=8G --end_fsync=1" "$STUB_LOG" && ok "fio fixed-work command line" || bad "fio command" "$(grep 'fio .*--rw' "$STUB_LOG" | tail -1)" "--io_size=8G --end_fsync=1"
[[ ! -e io_job.0.0 ]] && ok "fio file removed after the case" || bad "fio file" "exists" "removed"

run "$T/o3" c_seq rq2 sequential cpu_ram max 2 100 1 3 -- "cpu:threads=8,div=1" "ram:chunk_gb=8,div=1" > /dev/null
f="$T/o3/c_seq.csv"
eq "sequential: 2 windows" "$(rows "$f")" "2"; consistent "$f" "sequential"
eq "window 1 name" "$(col "$f" 1 window_name)" "cpu"; eq "window 2 name" "$(col "$f" 2 window_name)" "ram"

: > "$STUB_LOG"
run "$T/o4" c_ram4 rq3_ram parallel ram max 4 100 1 9 -- "ram:chunk_gb=2,div=4" "ram:chunk_gb=2,div=4" "ram:chunk_gb=2,div=4" "ram:chunk_gb=2,div=4" > /dev/null
f="$T/o4/c_ram4.csv"
eq "RQ3 RAM N=4: vm_ops_req = 4 x (300/4 x 6000)" "$(col "$f" 1 vm_ops_req)" "1800000"
eq "RQ3 RAM N=4: vm_gib_est = 300" "$(col "$f" 1 vm_gib_est)" "300.000"
eq "RQ3 RAM N=4 launches 4 workers" "$(grep -c -- '--vm 1 --vm-bytes 2G' "$STUB_LOG")" "4"

: > "$STUB_LOG"
run "$T/o5" c_half rq3_cpu parallel cpu load50 2 50 1 1 -- "cpu:threads=4,div=2" "cpu:threads=4,div=2" > /dev/null
f="$T/o5/c_half.csv"
eq "partial load: cpu_ops_req = 2 x 50000" "$(col "$f" 1 cpu_ops_req)" "100000"
eq "partial load passes --cpu-load 50" "$(grep -c -- '--cpu-load 50' "$STUB_LOG")" "2"

: > "$STUB_LOG"
run "$T/o6" c_pin mech parallel cpu_ram_pinned x 2 100 1 1 -- "cpu:threads=6,cpus=0-2;4-6,time=5" "ram:chunk_gb=8,cpus=3,time=5" > /dev/null
f="$T/o6/c_pin.csv"
eq "fixed-time components have no requested work" "$(col "$f" 1 cpu_ops_req)" ""
eq "pinned: taskset used twice" "$(grep -c '^taskset' "$STUB_LOG")" "2"
eq "pinned: cpu list with commas" "$(grep -c '^taskset -c 0-2,4-6 ' "$STUB_LOG")" "1"
eq "fixed-time: work_complete" "$(col "$f" 1 work_complete)" "1"

run "$T/o7" c_idle idle single idle 0 1 100 1 1 -- "idle:secs=1" > /dev/null
eq "idle: energy recorded" "$(col "$T/o7/c_idle.csv" 1 energy_j)" "0.000000"

STUB_SHORT=1 run "$T/o8" c_short rq1 single cpu 8 1 100 1 1 -- "cpu:threads=8,div=1" > /dev/null
eq "short run: work_complete = 0" "$(col "$T/o8/c_short.csv" 1 work_complete)" "0"

sed -i 's/^PERF_ON=0/PERF_ON=1/' "$T/calibration.env"
: > "$STUB_LOG"
run "$T/o9" c_perf rq1 single cpu 8 1 100 1 1 -- "cpu:threads=8,div=1" > /dev/null
f="$T/o9/c_perf.csv"
eq "perf: instructions" "$(col "$f" 1 instructions)" "1000000000"
eq "perf: dram_read_mib" "$(col "$f" 1 dram_read_mib)" "124.50"
eq "perf: dram_write_mib" "$(col "$f" 1 dram_write_mib)" "68.25"
CASE_PERF=off run "$T/o10" c_noperf rq1 single cpu 8 1 100 1 1 -- "cpu:threads=8,div=1" > /dev/null
eq "CASE_PERF=off: no instructions" "$(col "$T/o10/c_noperf.csv" 1 instructions)" ""
run "$T/o11" c_idle2 idle single idle 0 1 100 1 1 -- "idle:secs=1" > /dev/null
eq "idle window is not wrapped in perf" "$(col "$T/o11/c_idle2.csv" 1 instructions)" ""

echo
echo "passed: $PASS   failed: $FAIL"
[[ $FAIL -eq 0 ]]
