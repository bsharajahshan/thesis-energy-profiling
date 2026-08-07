#!/bin/bash

# measure_rapl.sh - RAPL Energy Measurement Wrapper

# Usage: measure_rapl.sh "command to run" "task_name" "params_json"
# Returns: duration_ns, energy_uj, average_power_w

COMMAND="$1"
TASK_NAME="$2"
PARAMS="$3"

# Verify RAPL path exists and is readable
RAPL_PATH="/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"
if [[ ! -f "$RAPL_PATH" ]]; then
    echo "WARNING: RAPL not available at $RAPL_PATH" >&2
    RAPL_AVAILABLE=0
elif [[ ! -r "$RAPL_PATH" ]]; then
    echo "WARNING: RAPL not readable (requires root). Run: sudo chmod 644 $RAPL_PATH" >&2
    RAPL_AVAILABLE=0
else
    RAPL_AVAILABLE=1
fi

# 1. Read RAPL counter before (or use 0 if unavailable)
if [[ $RAPL_AVAILABLE -eq 1 ]]; then
    ENERGY_BEFORE=$(cat "$RAPL_PATH")
else
    ENERGY_BEFORE=0
fi

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

# 3. Read RAPL counter after
END_TIME=$(date +%s%N)
if [[ $RAPL_AVAILABLE -eq 1 ]]; then
    ENERGY_AFTER=$(cat "$RAPL_PATH")
else
    ENERGY_AFTER=0
fi

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

# 4. Handle counter wraparound (RAPL wraps on long runs)
# If AFTER < BEFORE, the counter wrapped
if (( ENERGY_AFTER < ENERGY_BEFORE )); then
    # RAPL counter is 32-bit, max value ~4.3e9 µJ (~= 4300 J, or ~1W * 1200s max per wrap)
    ENERGY_MAX=4294967295  # 2^32 - 1
    ENERGY_UJ=$((ENERGY_MAX - ENERGY_BEFORE + ENERGY_AFTER))
else
    ENERGY_UJ=$((ENERGY_AFTER - ENERGY_BEFORE))
fi

DURATION_NS=$((END_TIME - START_TIME))
DURATION_S=$(echo "scale=3; $DURATION_NS / 1000000000" | bc)

# 5. Calculate average power
ENERGY_J=$(echo "scale=6; $ENERGY_UJ / 1000000" | bc)
AVG_POWER_W=$(echo "scale=3; $ENERGY_J / $DURATION_S" | bc)

# 6. Output results
echo "TASK_NAME=$TASK_NAME"
echo "DURATION_NS=$DURATION_NS"
echo "DURATION_S=$DURATION_S"
echo "ENERGY_UJ=$ENERGY_UJ"
echo "ENERGY_J=$ENERGY_J"
echo "POWER_W=$AVG_POWER_W"
echo "CPU_UTIL_PCT=$CPU_UTIL_PCT"
echo "MEM_UTIL_PCT=$MEM_UTIL_PCT"
echo "EXIT_CODE=$WORKLOAD_EXIT"

exit $WORKLOAD_EXIT
