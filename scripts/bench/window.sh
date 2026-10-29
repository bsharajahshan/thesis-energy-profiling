#!/usr/bin/env bash

# window.sh - the measured window of one run.
#
# Called by run_case.sh, either directly or under `perf stat -a`, so perf
# counts exactly this window plus a few milliseconds of process start.
#
# Usage: window.sh <session.env> <out_dir>
# Reads <out_dir>/plan.sh and part_<i>.sh (both made by benchlib.py plan).
# Writes into <out_dir>:
#   window.txt       time (bash EPOCHREALTIME) and RAPL package/DRAM counters at
#                    the start and end of the window, and of every part in a
#                    sequential run
#   part_<i>.out     the part's own output (stress-ng counters, fio messages)
#   part_<i>.time    "<start> <end> <exit code>" of the part
#   snap_before.txt / snap_after.txt   machine state right before / after the
#                    window: throttle counters, RAPL limits, governor, EPP,
#                    power cable, CPU time per cgroup, disk counters, memory
#   events.txt       lock_busy, watchdog_fired
# Exit 0 = the window ran (the parts' exit codes are in part_<i>.time),
#      3 = it did not run (another window holds the lock).

set -u
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec </dev/null

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_ENV="$1"
OUT="$2"
# shellcheck source=common.sh
source "$HERE/common.sh"
# shellcheck source=/dev/null
source "$SESSION_ENV"
# shellcheck source=/dev/null
source "$OUT/plan.sh"
W="$OUT/window.txt"

# Only one measurement window at a time on the whole machine. fd 9 is
# inherited by the parts, so a worker left over from this run would also
# keep the next window from starting instead of overlapping with it.
exec 9>>"$LOCK_FILE"
if ! "$FLOCK" -n 9; then
    event lock_busy
    exit 3
fi

# energy - RAPL package and DRAM counters into E_PKG / E_DRAM ('-' if unreadable).
energy() {
    rd E_PKG "$RAPL_PKG_DIR/energy_uj"
    E_DRAM=""
    [[ -n "${RAPL_DRAM_DIR:-}" ]] && rd E_DRAM "$RAPL_DRAM_DIR/energy_uj"
    E_PKG="${E_PKG:--}"
    E_DRAM="${E_DRAM:--}"
}

# snap FILE - machine state, one "key value..." line each (format read by benchlib.parse_snap).
snap() {
    local v x name c k dir tree cpu ms line
    {
        printf 'time_us %s\n' "$EPOCHREALTIME"
        rd v "$RAPL_PKG_DIR/energy_uj";             printf 'pkg_uj %s\n' "${v:--}"
        rd v "$RAPL_PKG_DIR/max_energy_range_uj";   printf 'pkg_max_uj %s\n' "${v:--}"
        if [[ -n "${RAPL_DRAM_DIR:-}" ]]; then
            rd v "$RAPL_DRAM_DIR/energy_uj";            printf 'dram_uj %s\n' "${v:--}"
            rd v "$RAPL_DRAM_DIR/max_energy_range_uj";  printf 'dram_max_uj %s\n' "${v:--}"
        fi
        rd v "$TEMP_FILE"; printf 'temp_mc %s\n' "${v:--}"
        if [[ -n "${AC_FILE:-}" ]]; then rd v "$AC_FILE"; printf 'ac %s\n' "${v:--}"; fi
        for c in "$CPU_SYSFS"/cpu[0-9]*; do
            cpu="${c##*/}"
            if [[ -e "$c/cpufreq/scaling_governor" ]]; then
                rd v "$c/cpufreq/scaling_governor"; printf 'gov %s %s\n' "$cpu" "${v:--}"
            fi
            if [[ -e "$c/cpufreq/energy_performance_preference" ]]; then
                rd v "$c/cpufreq/energy_performance_preference"; printf 'epp %s %s\n' "$cpu" "${v:--}"
            fi
            if [[ -e "$c/thermal_throttle/core_throttle_count" ]]; then
                rd x "$c/thermal_throttle/core_throttle_count"
                rd ms "$c/thermal_throttle/core_throttle_total_time_ms"
                printf 'thr_core %s %s %s\n' "$cpu" "${x:--}" "${ms:--}"
            fi
        done
        c="$CPU_SYSFS/cpu0/thermal_throttle"
        if [[ -e "$c/package_throttle_count" ]]; then
            rd v "$c/package_throttle_count";         printf 'thr_pkg_count %s\n' "${v:--}"
            rd v "$c/package_throttle_total_time_ms"; printf 'thr_pkg_ms %s\n' "${v:--}"
        fi
        for tree in pkg mmio; do
            dir="$RAPL_PKG_DIR"
            [[ "$tree" == mmio ]] && dir="${RAPL_MMIO_DIR:-}"
            [[ -n "$dir" ]] || continue
            for k in 0 1 2 3; do
                [[ -e "$dir/constraint_${k}_power_limit_uw" ]] || continue
                rd name "$dir/constraint_${k}_name"
                rd v "$dir/constraint_${k}_power_limit_uw"
                printf 'lim %s %s %s %s\n' "$tree" "$k" "${name:--}" "${v:--}"
            done
        done
        for x in "ours:${CGROUP_OURS:-}" "user:${CGROUP_ROOT:-}/user.slice" \
                 "system:${CGROUP_ROOT:-}/system.slice" "init:${CGROUP_ROOT:-}/init.scope"; do
            name="${x%%:*}"
            dir="${x#*:}"
            [[ -n "$dir" && -r "$dir/cpu.stat" ]] || continue
            while read -r k v; do
                [[ "$k" == usage_usec ]] && printf 'cg %s %s\n' "$name" "$v"
            done < "$dir/cpu.stat"
        done
        rd line "${PROC_STAT:-/proc/stat}"; printf 'procstat %s\n' "$line"
        if [[ -n "${DISK_STAT:-}" ]]; then rd line "$DISK_STAT"; printf 'disk %s\n' "$line"; fi
        while read -r k v x; do
            if [[ "$k" == MemAvailable: ]]; then printf 'memavail_kb %s\n' "$v"; break; fi
        done < "${PROC_MEMINFO:-/proc/meminfo}"
    } > "$1" 2>/dev/null
}

# run_part I - run part_I.sh, record its start, end and exit code.
run_part() {
    local i="$1" s e rc
    s="$EPOCHREALTIME"
    "$BASH" "$OUT/part_$i.sh" > "$OUT/part_$i.out" 2>&1
    rc=$?
    e="$EPOCHREALTIME"
    printf '%s %s %s\n' "$s" "$e" "$rc" > "$OUT/part_$i.time"
}

snap "$OUT/snap_before.txt"

t="$EPOCHREALTIME"
energy
printf 'win_start %s %s %s\n' "$t" "$E_PKG" "$E_DRAM" >> "$W"

# Watchdog: stops all parts if the window runs far longer than expected. It
# lets go of the lock (fd 9) so that its own `sleep` cannot block later windows.
(
    exec 9>&-
    sleep "$PLAN_WATCHDOG_S"
    event "watchdog_fired $EPOCHREALTIME"
    : > "$OUT/.watchdog_fired"
    kill_tree "$$" "$BASHPID"
) &
WD_PID=$!

if [[ "$PLAN_MODE" == parallel ]]; then
    pids=()
    for ((i = 0; i < PLAN_NPARTS; i++)); do
        run_part "$i" &
        pids+=($!)
    done
    wait "${pids[@]}"
else
    for ((i = 0; i < PLAN_NPARTS; i++)); do
        t="$EPOCHREALTIME"
        energy
        printf 'sub_start %s %s %s %s\n' "$i" "$t" "$E_PKG" "$E_DRAM" >> "$W"
        run_part "$i"
        energy
        t="$EPOCHREALTIME"
        printf 'sub_end %s %s %s %s\n' "$i" "$t" "$E_PKG" "$E_DRAM" >> "$W"
        [[ -e "$OUT/.watchdog_fired" ]] && break
    done
fi

energy
t="$EPOCHREALTIME"
printf 'win_end %s %s %s\n' "$t" "$E_PKG" "$E_DRAM" >> "$W"

stop_pid "$WD_PID"

snap "$OUT/snap_after.txt"
exit 0
