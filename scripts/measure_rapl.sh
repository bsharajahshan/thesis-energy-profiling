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
echo "EXIT_CODE=$WORKLOAD_EXIT"

exit $WORKLOAD_EXIT
