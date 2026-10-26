#!/bin/bash

# measure_rapl.sh - RAPL Energy Measurement Wrapper (v2, 2026-10-26)

# Usage: measure_rapl.sh "command to run" "task_name"
# Prints KEY=value lines: duration, package energy, dram energy, average power,
# CPU and memory utilisation, exit code, and the exact start/end time (ns since epoch).
#
# Reads both the package (PKG) domain and, if present, the dram sub-domain
# separately - added 2026-10-02 after Philipp confirmed the memory
# controller's energy is NOT counted in the package domain, only in dram.
#
# Changes in v2 (for the re-run of 2026-10):
#   - A lock (flock) is held for the whole measurement, so two measurement windows
#     can never overlap, whatever the workflow engine does. run_case.sh already holds
#     the lock for its whole case and sets RAPL_LOCK_HELD=1 so this script does not
#     lock a second time.
#   - START_NS / END_NS are printed, so a run can be joined exactly to the session logs.
#   - WORKLOAD_LOG (optional): file that receives the stdout and stderr of the workload.
#   - The memory sampler is pure bash (no awk process every 0.5 s); one sample per second.
#   - bc is no longer needed (awk does the arithmetic).
#   - RAPL_ROOT (optional) points to another sysfs tree, for tests.

# Force a locale with '.' as the decimal separator (de_DE would print ',').
export LC_ALL=C

COMMAND="$1"
TASK_NAME="$2"

RAPL_ROOT="${RAPL_ROOT:-/sys/class/powercap/intel-rapl}"
RAPL_PKG_PATH="$RAPL_ROOT/intel-rapl:0/energy_uj"
RAPL_PKG_MAX_PATH="$RAPL_ROOT/intel-rapl:0/max_energy_range_uj"
if [[ ! -f "$RAPL_PKG_PATH" ]]; then
    echo "WARNING: RAPL not available at $RAPL_PKG_PATH" >&2
    RAPL_AVAILABLE=0
elif [[ ! -r "$RAPL_PKG_PATH" ]]; then
    echo "WARNING: RAPL not readable (requires root). Run: sudo chmod 644 $RAPL_PKG_PATH" >&2
    RAPL_AVAILABLE=0
else
    RAPL_AVAILABLE=1
fi

# Locate the dram sub-domain by name instead of a fixed number.
RAPL_DRAM_PATH=""
RAPL_DRAM_MAX_PATH=""
RAPL_DRAM_UNREADABLE_PATH=""
if [[ $RAPL_AVAILABLE -eq 1 ]]; then
    for d in "$RAPL_ROOT"/intel-rapl:0/intel-rapl:0:*; do
        [[ -d "$d" ]] || continue
        [[ "$(cat "$d/name" 2>/dev/null)" == "dram" ]] || continue
        if [[ -r "$d/energy_uj" ]]; then
            RAPL_DRAM_PATH="$d/energy_uj"
            RAPL_DRAM_MAX_PATH="$d/max_energy_range_uj"
        else
            RAPL_DRAM_UNREADABLE_PATH="$d/energy_uj"
        fi
        break
    done
    if [[ -z "$RAPL_DRAM_PATH" ]] && [[ -n "$RAPL_DRAM_UNREADABLE_PATH" ]]; then
        echo "WARNING: dram RAPL domain found but not readable (requires root). Run: sudo chmod 644 $RAPL_DRAM_UNREADABLE_PATH" >&2
    elif [[ -z "$RAPL_DRAM_PATH" ]]; then
        echo "WARNING: no dram RAPL sub-domain found under intel-rapl:0 on this machine - DRAM_ENERGY_UJ will be empty" >&2
    fi
fi

# Hard guarantee that measurement windows never overlap (see header).
if [[ -z "${RAPL_LOCK_HELD:-}" ]]; then
    if command -v flock >/dev/null 2>&1; then
        exec 9>"${RAPL_LOCK_FILE:-/tmp/thesis_rapl_measure.lock}"
        flock 9
    else
        echo "WARNING: flock not found - measurement windows are not protected against overlap" >&2
    fi
fi

# Reads a RAPL energy counter, or 0 if the path is empty/unavailable.
read_rapl_uj() {
    local path="$1"
    if [[ -n "$path" ]] && [[ -r "$path" ]]; then
        cat "$path"
    else
        echo 0
    fi
}

# One domain's energy delta, with wraparound handled by THAT domain's own
# max_energy_range_uj read at runtime (the old hardcoded 2^32-1 gave a negative
# energy value on 2026-09-25).
rapl_delta_uj() {
    local before="$1" after="$2" max_path="$3"
    if (( after < before )); then
        local max_uj
        if [[ -n "$max_path" ]] && [[ -r "$max_path" ]]; then
            max_uj=$(cat "$max_path")
        else
            max_uj=4294967295
        fi
        echo $(( max_uj - before + after ))
    else
        echo $(( after - before ))
    fi
}

# 1. Read RAPL counters before (package always; dram if available)
PKG_ENERGY_BEFORE=$(read_rapl_uj "$RAPL_PKG_PATH")
DRAM_ENERGY_BEFORE=$(read_rapl_uj "$RAPL_DRAM_PATH")

# Whole-system CPU and memory utilisation, read from /proc (no sysstat needed).
CPU_STAT_BEFORE=$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)

MEM_SAMPLE_FILE=$(mktemp)
(
    while true; do
        t=0; a=0
        while read -r k v _; do
            case "$k" in
                MemTotal:) t=$v ;;
                MemAvailable:) a=$v ;;
            esac
        done < /proc/meminfo
        echo "$t $a"
        sleep 1
    done
) > "$MEM_SAMPLE_FILE" 2>/dev/null &
MEM_SAMPLER_PID=$!
trap 'kill $MEM_SAMPLER_PID 2>/dev/null' EXIT

START_NS=$(date +%s%N)

# 2. Run the workload (fd 9 = lock is closed for the workload)
if [[ -n "${WORKLOAD_LOG:-}" ]]; then
    eval "$COMMAND" > "$WORKLOAD_LOG" 2>&1 9>&-
else
    eval "$COMMAND" 9>&-
fi
WORKLOAD_EXIT=$?

# 3. Read RAPL counters after
END_NS=$(date +%s%N)
PKG_ENERGY_AFTER=$(read_rapl_uj "$RAPL_PKG_PATH")
DRAM_ENERGY_AFTER=$(read_rapl_uj "$RAPL_DRAM_PATH")

kill "$MEM_SAMPLER_PID" 2>/dev/null
wait "$MEM_SAMPLER_PID" 2>/dev/null
trap - EXIT

CPU_STAT_AFTER=$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)

# CPU utilisation = 1 - (idle+iowait delta)/(total delta) over the whole window,
# whole-system, not per process.
CPU_UTIL_PCT=$(awk -v before="$CPU_STAT_BEFORE" -v after="$CPU_STAT_AFTER" 'BEGIN {
    split(before, b, " "); split(after, a, " ")
    idle_b = b[4] + b[5]; idle_a = a[4] + a[5]
    total_b = b[1]+b[2]+b[3]+b[4]+b[5]+b[6]+b[7]
    total_a = a[1]+a[2]+a[3]+a[4]+a[5]+a[6]+a[7]
    dt = total_a - total_b
    di = idle_a - idle_b
    if (dt > 0) printf "%.3f", 100 * (1 - di/dt); else print "0"
}')

# Memory utilisation = average % used across the samples taken during the run
MEM_UTIL_PCT=$(awk '$1 > 0 {sum += 100 * (1 - $2/$1); n++} END {if (n>0) printf "%.3f", sum/n; else print "0"}' "$MEM_SAMPLE_FILE")
rm -f "$MEM_SAMPLE_FILE"

# 4. Counter wraparound per domain
ENERGY_UJ=$(rapl_delta_uj "$PKG_ENERGY_BEFORE" "$PKG_ENERGY_AFTER" "$RAPL_PKG_MAX_PATH")
if [[ -n "$RAPL_DRAM_PATH" ]]; then
    DRAM_ENERGY_UJ=$(rapl_delta_uj "$DRAM_ENERGY_BEFORE" "$DRAM_ENERGY_AFTER" "$RAPL_DRAM_MAX_PATH")
else
    DRAM_ENERGY_UJ=""
fi

DURATION_NS=$((END_NS - START_NS))
DURATION_S=$(awk -v n="$DURATION_NS" 'BEGIN { printf "%.3f", n / 1000000000 }')

# 5. Energy in Joules and average package power
ENERGY_J=$(awk -v u="$ENERGY_UJ" 'BEGIN { printf "%.6f", u / 1000000 }')
AVG_POWER_W=$(awk -v e="$ENERGY_J" -v d="$DURATION_S" 'BEGIN { if (d > 0) printf "%.3f", e / d; else print "0" }')
if [[ -n "$DRAM_ENERGY_UJ" ]]; then
    DRAM_ENERGY_J=$(awk -v u="$DRAM_ENERGY_UJ" 'BEGIN { printf "%.6f", u / 1000000 }')
else
    DRAM_ENERGY_J=""
fi

# 6. Output results
echo "TASK_NAME=$TASK_NAME"
echo "DURATION_NS=$DURATION_NS"
echo "DURATION_S=$DURATION_S"
echo "ENERGY_UJ=$ENERGY_UJ"
echo "ENERGY_J=$ENERGY_J"
echo "DRAM_ENERGY_UJ=$DRAM_ENERGY_UJ"
echo "DRAM_ENERGY_J=$DRAM_ENERGY_J"
echo "POWER_W=$AVG_POWER_W"
echo "CPU_UTIL_PCT=$CPU_UTIL_PCT"
echo "MEM_UTIL_PCT=$MEM_UTIL_PCT"
echo "START_NS=$START_NS"
echo "END_NS=$END_NS"
echo "EXIT_CODE=$WORKLOAD_EXIT"

exit $WORKLOAD_EXIT
