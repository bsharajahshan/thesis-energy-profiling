#!/bin/bash

# measure_rapl.sh - RAPL Energy Measurement Wrapper

# Usage: measure_rapl.sh "command to run" "task_name" "params_json"
# Returns: duration_ns, package energy_uj (+ dram energy_uj if available), average_power_w
#
# Reads both the package (PKG) domain and, if present, the dram sub-domain
# separately - added 2026-10-02 after Philipp confirmed the memory
# controller's energy is NOT counted in the package domain, only in dram
# (contrary to this project's earlier assumption). Package alone can no
# longer answer "how much did RAM use" - both numbers are needed together.

# Force a locale with '.' as the decimal separator. Under e.g. de_DE.UTF-8,
# awk/bc emit ',' instead (e.g. "93,715" instead of "93.715"), which corrupts
# the CSV (extra fields) and makes bc choke on comma-decimal arithmetic.
export LC_ALL=C

COMMAND="$1"
TASK_NAME="$2"
PARAMS="$3"

# Verify RAPL package path exists and is readable
RAPL_PKG_PATH="/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"
RAPL_PKG_MAX_PATH="/sys/class/powercap/intel-rapl/intel-rapl:0/max_energy_range_uj"
if [[ ! -f "$RAPL_PKG_PATH" ]]; then
    echo "WARNING: RAPL not available at $RAPL_PKG_PATH" >&2
    RAPL_AVAILABLE=0
elif [[ ! -r "$RAPL_PKG_PATH" ]]; then
    echo "WARNING: RAPL not readable (requires root). Run: sudo chmod 644 $RAPL_PKG_PATH" >&2
    RAPL_AVAILABLE=0
else
    RAPL_AVAILABLE=1
fi

# Locate the dram sub-domain by name rather than hardcoding its numeric
# suffix. Confirmed as intel-rapl:0:2 on this project's test machine (via
# turbostat, 2026-10-01), but the enumeration order isn't guaranteed to
# match on a different machine or kernel version, so search intel-rapl:0's
# sub-domains for whichever one's `name` file actually reads "dram".
RAPL_DRAM_PATH=""
RAPL_DRAM_MAX_PATH=""
RAPL_DRAM_UNREADABLE_PATH=""  # set if a dram domain exists but chmod hasn't been applied to it
if [[ $RAPL_AVAILABLE -eq 1 ]]; then
    for d in /sys/class/powercap/intel-rapl/intel-rapl:0/intel-rapl:0:*; do
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

# Reads a RAPL energy counter, or 0 if the path is empty/unavailable.
read_rapl_uj() {
    local path="$1"
    if [[ -n "$path" ]] && [[ -r "$path" ]]; then
        cat "$path"
    else
        echo 0
    fi
}

# Computes one domain's energy delta, handling counter wraparound using
# THAT domain's own max_energy_range_uj read at runtime, instead of a
# hardcoded 2^32-1 assumption. The old hardcoded constant produced a garbage
# negative energy value on a real run (RQ3 RAM sequential N=8 rep 1,
# 2026-09-25) because the real wraparound range was roughly 60x larger than
# assumed - reading the actual value from sysfs avoids repeating that.
rapl_delta_uj() {
    local before="$1" after="$2" max_path="$3"
    if (( after < before )); then
        local max_uj
        if [[ -n "$max_path" ]] && [[ -r "$max_path" ]]; then
            max_uj=$(cat "$max_path")
        else
            max_uj=4294967295  # 2^32 - 1, fallback only if max_energy_range_uj is unreadable
        fi
        echo $(( max_uj - before + after ))
    else
        echo $(( after - before ))
    fi
}

# 1. Read RAPL counters before (package always; dram if available)
PKG_ENERGY_BEFORE=$(read_rapl_uj "$RAPL_PKG_PATH")
DRAM_ENERGY_BEFORE=$(read_rapl_uj "$RAPL_DRAM_PATH")

# Resource utilisation sampling (CPU%, memory%) — read directly from /proc so
# there's no dependency on sysstat (mpstat/vmstat) being installed. This is
# both a stress-verification signal (proof the workload actually loaded the
# system) and, for RQ2, a required metric in its own right.
CPU_STAT_BEFORE=$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)

MEM_SAMPLE_FILE=$(mktemp)
(
    while true; do
        awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {if (t>0) print 100*(1-a/t)}' /proc/meminfo
        sleep 0.5
    done
) > "$MEM_SAMPLE_FILE" &
MEM_SAMPLER_PID=$!
trap 'kill $MEM_SAMPLER_PID 2>/dev/null' EXIT

START_TIME=$(date +%s%N)

# 2. Run the workload
eval "$COMMAND"
WORKLOAD_EXIT=$?

# 3. Read RAPL counters after
END_TIME=$(date +%s%N)
PKG_ENERGY_AFTER=$(read_rapl_uj "$RAPL_PKG_PATH")
DRAM_ENERGY_AFTER=$(read_rapl_uj "$RAPL_DRAM_PATH")

# Stop utilisation sampling and compute results
kill "$MEM_SAMPLER_PID" 2>/dev/null
wait "$MEM_SAMPLER_PID" 2>/dev/null
trap - EXIT

CPU_STAT_AFTER=$(awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8}' /proc/stat)

# CPU utilisation = 1 - (idle+iowait delta)/(total delta), averaged over the
# whole measurement window. Whole-system aggregate across all cores, not
# per-process attribution.
CPU_UTIL_PCT=$(awk -v before="$CPU_STAT_BEFORE" -v after="$CPU_STAT_AFTER" 'BEGIN {
    split(before, b, " "); split(after, a, " ")
    idle_b = b[4] + b[5]; idle_a = a[4] + a[5]
    total_b = b[1]+b[2]+b[3]+b[4]+b[5]+b[6]+b[7]
    total_a = a[1]+a[2]+a[3]+a[4]+a[5]+a[6]+a[7]
    dt = total_a - total_b
    di = idle_a - idle_b
    if (dt > 0) printf "%.3f", 100 * (1 - di/dt); else print "0"
}')

# Memory utilisation = average % used across samples taken during the run
MEM_UTIL_PCT=$(awk '{sum+=$1; n++} END {if (n>0) printf "%.3f", sum/n; else print "0"}' "$MEM_SAMPLE_FILE")
rm -f "$MEM_SAMPLE_FILE"

# 4. Handle counter wraparound (RAPL wraps on long runs) per domain
ENERGY_UJ=$(rapl_delta_uj "$PKG_ENERGY_BEFORE" "$PKG_ENERGY_AFTER" "$RAPL_PKG_MAX_PATH")
if [[ -n "$RAPL_DRAM_PATH" ]]; then
    DRAM_ENERGY_UJ=$(rapl_delta_uj "$DRAM_ENERGY_BEFORE" "$DRAM_ENERGY_AFTER" "$RAPL_DRAM_MAX_PATH")
else
    DRAM_ENERGY_UJ=""
fi

DURATION_NS=$((END_TIME - START_TIME))
DURATION_S=$(echo "scale=3; $DURATION_NS / 1000000000" | bc)

# 5. Calculate energy in Joules and average package power
ENERGY_J=$(echo "scale=6; $ENERGY_UJ / 1000000" | bc)
AVG_POWER_W=$(echo "scale=3; $ENERGY_J / $DURATION_S" | bc)
if [[ -n "$DRAM_ENERGY_UJ" ]]; then
    DRAM_ENERGY_J=$(echo "scale=6; $DRAM_ENERGY_UJ / 1000000" | bc)
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
echo "EXIT_CODE=$WORKLOAD_EXIT"

exit $WORKLOAD_EXIT
