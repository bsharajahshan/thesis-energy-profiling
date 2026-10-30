# fake_machine.sh - a fake measurement laptop for the shell tests. Sourced, not executed.
#
#   fake_machine_start DIR   creates under DIR:
#       root/   sys, proc and etc files in the real Linux layout (BENCH_SYSROOT for
#               preflight.py): RAPL behind a symlink like on the laptop, hwmon
#               coretemp, 8 CPUs with sibling pairs 0-4 1-5 2-6 3-7, power supply,
#               cgroup v2, /proc/stat, meminfo, self/cgroup
#       bin/    stand-in tools: stress-ng, fio, perf, taskset (real output formats)
#       ctl/    control files and call logs of the stand-ins
#       session.env  for run_case.sh tests (preflight.py writes its own)
#     and starts the ticker
#   make_env                 rewrites $T/session.env (extra KEY='value' lines on stdin)
#   fake_machine_stop        stops the ticker
#
# Sets T, R (root), CTL, BIN and the paths F_PKG, F_DRAM, F_TEMP, F_AC, F_BAT, F_CPU, F_CG.
# The ticker raises the RAPL counters by 2.5 J (package) and 0.1 J (DRAM) every
# 0.1 s (25 W / 1 W at most), gives "our" cgroup half a CPU and other programs
# $CTL/system_rate microseconds of CPU per tick.
# Control files: stress_mode (short | hang | fail), short_stressors (one stressor
# name per line, e.g. vm: only those report too few operations), stress_secs
# (seconds a fixed-work stress-ng run takes, default 1), perf_mode
# (broken), ops_semantics (per_instance), system_rate. Stand-ins write their
# command lines to $CTL/<tool>_calls, and anything that reads stdin or has a
# terminal to $CTL/violations.

put() { printf '%s\n' "$2" > "$1.tmp" && mv -f "$1.tmp" "$1"; }

fake_machine_start() {
    T="$1"
    R="$T/root"
    CTL="$T/ctl"
    BIN="$T/bin"
    mkdir -p "$CTL" "$BIN" "$T/io"
    touch "$CTL/stress_calls" "$CTL/fio_calls" "$CTL/perf_calls" "$CTL/taskset_calls" "$CTL/violations"

    local rapl="$R/sys/devices/virtual/powercap/intel-rapl" mmio="$R/sys/devices/virtual/powercap/intel-rapl-mmio"
    F_PKG="$rapl/intel-rapl:0"
    F_DRAM="$F_PKG/intel-rapl:0:2"
    mkdir -p "$F_PKG/intel-rapl:0:0" "$F_DRAM" "$mmio/intel-rapl-mmio:0" "$R/sys/class/powercap"
    put "$F_PKG/name" package-0
    put "$F_PKG/energy_uj" 1000000
    put "$F_PKG/max_energy_range_uj" 262143328850
    put "$F_PKG/constraint_0_name" long_term
    put "$F_PKG/constraint_0_power_limit_uw" 200000000
    put "$F_PKG/constraint_1_name" short_term
    put "$F_PKG/constraint_1_power_limit_uw" 44000000
    put "$F_PKG/intel-rapl:0:0/name" core
    put "$F_DRAM/name" dram
    put "$F_DRAM/energy_uj" 500000
    put "$F_DRAM/max_energy_range_uj" 262143328850
    put "$mmio/intel-rapl-mmio:0/name" package-0
    put "$mmio/intel-rapl-mmio:0/constraint_0_name" long_term
    put "$mmio/intel-rapl-mmio:0/constraint_0_power_limit_uw" 15000000
    ln -sfn ../../devices/virtual/powercap/intel-rapl/intel-rapl:0 "$R/sys/class/powercap/intel-rapl:0"
    ln -sfn ../../devices/virtual/powercap/intel-rapl-mmio/intel-rapl-mmio:0 "$R/sys/class/powercap/intel-rapl-mmio:0"

    mkdir -p "$R/sys/class/hwmon/hwmon2" "$R/sys/class/hwmon/hwmon4"
    put "$R/sys/class/hwmon/hwmon2/name" acpitz
    put "$R/sys/class/hwmon/hwmon2/temp1_input" 40000
    put "$R/sys/class/hwmon/hwmon4/name" coretemp
    put "$R/sys/class/hwmon/hwmon4/temp1_label" "Package id 0"
    put "$R/sys/class/hwmon/hwmon4/temp2_label" "Core 0"
    put "$R/sys/class/hwmon/hwmon4/temp2_input" 49000
    F_TEMP="$R/sys/class/hwmon/hwmon4/temp1_input"
    put "$F_TEMP" 50000

    F_CPU="$R/sys/devices/system/cpu"
    local c d f
    mkdir -p "$F_CPU/intel_pstate"
    put "$F_CPU/online" 0-7
    put "$F_CPU/intel_pstate/status" active
    put "$F_CPU/intel_pstate/no_turbo" 0
    for c in 0 1 2 3 4 5 6 7; do
        d="$F_CPU/cpu$c"
        mkdir -p "$d/cpufreq" "$d/thermal_throttle" "$d/topology"
        put "$d/cpufreq/scaling_governor" powersave
        put "$d/cpufreq/energy_performance_preference" balance_performance
        put "$d/cpufreq/scaling_available_governors" "performance powersave"
        put "$d/cpufreq/energy_performance_available_preferences" "default performance balance_performance balance_power power"
        put "$d/topology/thread_siblings_list" "$((c % 4)),$((c % 4 + 4))"
        for f in core_throttle_count core_throttle_total_time_ms package_throttle_count package_throttle_total_time_ms; do
            put "$d/thermal_throttle/$f" 0
        done
    done

    mkdir -p "$R/sys/class/power_supply/AC" "$R/sys/class/power_supply/BAT0"
    put "$R/sys/class/power_supply/AC/type" Mains
    put "$R/sys/class/power_supply/BAT0/type" Battery
    F_AC="$R/sys/class/power_supply/AC/online"
    F_BAT="$R/sys/class/power_supply/BAT0/capacity"
    put "$F_AC" 1
    put "$F_BAT" 80

    F_CG="$R/sys/fs/cgroup"
    mkdir -p "$F_CG/user.slice/ours" "$F_CG/system.slice" "$F_CG/init.scope"
    put "$F_CG/cgroup.controllers" "cpuset cpu io memory pids"
    local g
    for g in user.slice/ours user.slice system.slice init.scope; do
        put "$F_CG/$g/cpu.stat" "usage_usec 0"
    done

    mkdir -p "$R/proc/self" "$R/proc/sys/kernel" "$R/sys/block/sdb" "$R/etc/thesis-bench"
    put "$R/proc/stat" "cpu  100 0 50 1000 5 0 1 0 0 0"
    printf 'MemTotal:       16000000 kB\nMemFree:         9000000 kB\nMemAvailable:   13000000 kB\n' > "$R/proc/meminfo"
    put "$R/proc/self/cgroup" "0::/user.slice/ours"
    put "$R/proc/sys/kernel/perf_event_paranoid" -1
    put "$R/sys/block/sdb/stat" "100 0 200 300 400 0 1000 500 0 600 900 0 0 0 0 0 0"
    put "$R/etc/thesis-bench/setup.txt" "version=1 (fake)"
    put "$CTL/system_rate" 1000

    _fake_stubs
    make_env < /dev/null
    _fake_ticker &
    TICKER=$!
}

fake_machine_stop() {
    [[ -n "${TICKER:-}" ]] && kill "$TICKER" 2>/dev/null
    TICKER=""
}

_fake_ticker() {
    local p d m o u s rate
    while :; do
        read -r p < "$F_PKG/energy_uj"; read -r m < "$F_PKG/max_energy_range_uj"
        # a test sets the counter through $CTL/pkg_set, so only the ticker writes energy_uj
        if [[ -s "$CTL/pkg_set" ]]; then read -r p < "$CTL/pkg_set"; rm -f "$CTL/pkg_set"; fi
        read -r d < "$F_DRAM/energy_uj"; read -r rate < "$CTL/system_rate"
        p=$((p + 2500000)); (( p >= m )) && p=$((p - m))
        put "$F_PKG/energy_uj" "$p"
        put "$F_DRAM/energy_uj" "$((d + 100000))"
        read -r _ o < "$F_CG/user.slice/ours/cpu.stat"
        read -r _ u < "$F_CG/user.slice/cpu.stat"
        read -r _ s < "$F_CG/system.slice/cpu.stat"
        put "$F_CG/user.slice/ours/cpu.stat" "usage_usec $((o + 50000))"
        put "$F_CG/user.slice/cpu.stat" "usage_usec $((u + 50000))"
        put "$F_CG/system.slice/cpu.stat" "usage_usec $((s + rate))"
        sleep 0.1
    done
}

make_env() {
    {
        cat <<EOF
STRESS_NG='$BIN/stress-ng'
FIO='$BIN/fio'
PERF='$BIN/perf'
TASKSET='$BIN/taskset'
PYTHON='$(command -v python3)'
FLOCK='$(command -v flock)'
SLEEP_BIN='$(command -v sleep)'
RAPL_PKG_DIR='$F_PKG'
RAPL_DRAM_DIR='$F_DRAM'
RAPL_MMIO_DIR='$R/sys/devices/virtual/powercap/intel-rapl-mmio/intel-rapl-mmio:0'
TEMP_FILE='$F_TEMP'
AC_FILE='$F_AC'
CPU_SYSFS='$F_CPU'
CGROUP_ROOT='$F_CG'
CGROUP_OURS='$F_CG/user.slice/ours'
PROC_STAT='$R/proc/stat'
PROC_MEMINFO='$R/proc/meminfo'
DISK_STAT='$R/sys/block/sdb/stat'
LOCK_FILE='$T/window.lock'
IO_DIR='$T/io'
PERF_EVENTS='instructions,cycles,ref-cycles,msr/tsc/,uncore_imc/data_reads/,uncore_imc/data_writes/,power/energy-pkg/,power/energy-ram/,context-switches,cpu-migrations'
NCPU='8'
CPU_SIBLINGS='0,4;1,5;2,6;3,7'
SETTLE_TEMP_C='55'
SETTLE_MIN_S='0'
SETTLE_MAX_S='3'
SETTLE_FALLBACK_S='1'
AC_WAIT_MAX_S='2'
WATCHDOG_FACTOR='3'
WATCHDOG_MIN_EXTRA_S='5'
DISK_KEEP_FREE_KB='1024'
EOF
        cat
    } > "$T/session.env"
}

_fake_stubs() {
    cat > "$BIN/stress-ng" <<'EOF'
#!/bin/bash
CTL=__CTL__
if [[ "$1" == --version ]]; then echo "stress-ng, version 0.17.06 (gcc 13.2.0, x86_64 Linux stand-in)"; exit 0; fi
echo "$*" >> "$CTL/stress_calls"
mode=normal; [[ -f $CTL/stress_mode ]] && mode=$(<"$CTL/stress_mode")
sem=total; [[ -f $CTL/ops_semantics ]] && sem=$(<"$CTL/ops_semantics")
stressor="" n=1 ops="" timeout=""
while (( $# )); do
    case "$1" in
        --cpu|--vm) stressor=${1#--}; n=$2; shift ;;
        --cpu-ops|--vm-ops) ops=$2; shift ;;
        --timeout) timeout=${2%s}; shift ;;
        --cpu-method|--vm-method|--vm-bytes|--cpu-load) shift ;;
    esac
    shift
done
[[ -f $CTL/short_stressors ]] && grep -qx "$stressor" "$CTL/short_stressors" && mode=short
if read -r -t 0.2 line; then echo "stress-ng read stdin: $line" >> "$CTL/violations"; fi
[[ -t 0 ]] && echo "stress-ng has a terminal on stdin" >> "$CTL/violations"
pid=$$
echo "stress-ng: info:  [$pid] setting to a 1 min, 0 secs run per stressor"
echo "stress-ng: info:  [$pid] dispatching hogs: $n $stressor"
case $mode in
    hang) sleep 1000; exit 0 ;;
    fail) echo "stress-ng: fail:  [$pid] stand-in failure"; exit 2 ;;
esac
secs=1; [[ -f $CTL/stress_secs ]] && secs=$(<"$CTL/stress_secs")     # fixed-work run time
[[ -z "$ops" ]] && secs=${timeout:-1}
sleep "$secs"
done_ops=${ops:-4242}
[[ $sem == per_instance && -n "$ops" ]] && done_ops=$((ops * n))
[[ $mode == short ]] && done_ops=$((done_ops - 1))
printf 'stress-ng: metrc: [%s] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s\n' $pid
printf 'stress-ng: metrc: [%s]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)\n' $pid
printf 'stress-ng: metrc: [%s] %-12s %10d %9.2f %9.2f %9.2f %12.2f %14.2f\n' $pid "$stressor" "$done_ops" "$secs" "$secs" 0.01 "$done_ops" "$done_ops"
printf 'stress-ng: info:  [%s] skipped: 0\nstress-ng: info:  [%s] passed: %s: %s (%s)\n' $pid $pid "$n" "$stressor" "$n"
printf 'stress-ng: info:  [%s] failed: 0\nstress-ng: info:  [%s] metrics untrustworthy: 0\n' $pid $pid
printf 'stress-ng: info:  [%s] successful run completed in %s secs\n' $pid "$secs"
EOF

    cat > "$BIN/fio" <<'EOF'
#!/bin/bash
CTL=__CTL__
if [[ "$1" == --version ]]; then echo "fio-3.42"; exit 0; fi
echo "$*" >> "$CTL/fio_calls"
file="" size="" io_size="" out="" create=0
for a in "$@"; do
    case "$a" in
        --filename=*) file=${a#*=} ;;
        --size=*) size=${a#*=} ;;
        --io_size=*) io_size=${a#*=} ;;
        --output=*) out=${a#*=} ;;
        --create_only=1) create=1 ;;
    esac
done
if (( create )); then truncate -s "$size" "$file"; exit $?; fi
[[ -f "$file" ]] || echo "fio: test file missing when the window started" >> "$CTL/violations"
sleep 1
cat > "$out" <<JSON
{"fio version": "fio-3.42", "jobs": [{"jobname": "io_job", "error": 0,
 "write": {"io_bytes": $io_size, "iops": 28880.0, "runtime": 1000, "total_ios": $((io_size / 4096))},
 "usr_cpu": 3.4, "sys_cpu": 10.7, "job_runtime": 1000, "elapsed": 1}],
 "disk_util": [{"name": "sdb", "util": 90.7}]}
JSON
EOF

    cat > "$BIN/perf" <<'EOF'
#!/bin/bash
CTL=__CTL__
if [[ "$1" == --version ]]; then echo "perf version 7.0.stand-in"; exit 0; fi
echo "$*" >> "$CTL/perf_calls"
if [[ -f $CTL/perf_mode && $(<"$CTL/perf_mode") == broken ]]; then
    echo "event syntax error: 'uncore_imc/data_reads/'" >&2
    exit 129
fi
out="" events="" interval="" summary=0
while (( $# )); do
    case "$1" in
        -o) out=$2; shift ;;
        -e) events=$2; shift ;;
        -I) interval=$2; shift ;;
        --summary) summary=1 ;;
        --) shift; break ;;
    esac
    shift
done
start=$EPOCHREALTIME
"$@"
rc=$?
end=$EPOCHREALTIME
n=$(( ${end%.*} - ${start%.*} )); (( n < 1 )) && n=1
IFS=, read -ra evs <<< "$events"
value() {
    case $1 in
        uncore_imc/*) echo "1000.00 MiB" ;;
        power/*) echo "20.00 Joules" ;;
        msr/tsc/) echo "8000000000 -" ;;
        ref-cycles) echo "1000000000 -" ;;
        cycles) echo "1500000000 -" ;;
        *) echo "1000000 -" ;;
    esac
}
{
    printf '# started on stand-in\n\n'
    if [[ -n "$interval" ]]; then
        for ((k = 1; k <= n; k++)); do
            for e in "${evs[@]}"; do
                read -r v u <<< "$(value "$e")"; [[ $u == - ]] && u=""
                printf '%d.000100000,%s,%s,%s,1000000000,100.00,,\n' "$k" "$v" "$u" "$e"
            done
        done
    fi
    if [[ -z "$interval" || $summary == 1 ]]; then
        for e in "${evs[@]}"; do
            read -r v u <<< "$(value "$e")"; [[ $u == - ]] && u=""
            [[ -n "$interval" ]] && printf 'summary,'
            printf '%s,%s,%s,%d,100.00,,\n' "$(awk -v v="$v" -v n="$n" 'BEGIN { printf "%.2f", v * n }')" "$u" "$e" "$n"
        done
    fi
} > "$out"
exit $rc
EOF

    cat > "$BIN/taskset" <<'EOF'
#!/bin/bash
CTL=__CTL__
echo "$*" >> "$CTL/taskset_calls"
[[ "$1" == -c ]] && shift 2
exec "$@"
EOF

    local f
    for f in "$BIN"/*; do
        sed -i "s|__CTL__|$CTL|" "$f"
        chmod +x "$f"
    done
}
