#!/bin/bash

# validate_rapl.sh - Cross-validate RAPL readings against turbostat
#
# Measures the SAME single execution of a workload two independent ways:
#   1. Direct RAPL sysfs read (same method as measure_rapl.sh)
#   2. turbostat wrapping the identical command (separate code path, same
#      underlying RAPL/MSR hardware counters)
# Both are non-destructive counter reads, so running them concurrently around
# the same execution does not perturb either reading. Repeats for N reps and
# reports the discrepancy between the two per rep, plus a summary at the end.
#
# Usage:
#   sudo scripts/validate_rapl.sh "<command>" <task_name> [repetitions]
#
# Example:
#   sudo scripts/validate_rapl.sh "stress-ng --cpu 4 --timeout 20s" cpu_check 5
#
# Requires: root (turbostat needs raw MSR access), turbostat installed,
# msr kernel module loaded, RAPL sysfs counter readable.

set -uo pipefail

# Force a locale with '.' as the decimal separator — see measure_rapl.sh for
# why (de_DE.UTF-8 etc. make awk/bc emit ',' and corrupt the CSV/arithmetic).
export LC_ALL=C

COMMAND="$1"
TASK_NAME="${2:-validation_task}"
REPS="${3:-5}"

RAPL_PATH="/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"
ENERGY_MAX=4294967295  # 2^32 - 1, matches wraparound handling in measure_rapl.sh
OUT_DIR="${RESULTS_DIR:-results/raw/validation}"
OUT_FILE="${OUT_DIR}/rapl_validation.csv"
TOLERANCE_PCT=10  # flag any rep where RAPL and turbostat disagree by more than this

command -v turbostat >/dev/null 2>&1 || {
    echo "ERROR: turbostat not found. Install via:" >&2
    echo "  sudo apt-get install linux-tools-common linux-tools-\$(uname -r)" >&2
    exit 1
}

[[ -r "$RAPL_PATH" ]] || {
    echo "ERROR: RAPL not readable at $RAPL_PATH." >&2
    echo "  Run this script as root, or: sudo chmod 644 $RAPL_PATH" >&2
    exit 1
}

[[ -e /dev/cpu/0/msr ]] || echo "WARNING: /dev/cpu/0/msr missing — run: sudo modprobe msr" >&2

mkdir -p "$OUT_DIR"
if [[ ! -f "$OUT_FILE" ]]; then
    echo "rep,task_name,duration_s,rapl_energy_j,turbostat_pkg_energy_j,turbostat_ram_energy_j,delta_pct,timestamp" > "$OUT_FILE"
fi

echo "Validating RAPL against turbostat: ${REPS} rep(s) of: ${COMMAND}"

for ((rep = 1; rep <= REPS; rep++)); do
    ENERGY_BEFORE=$(cat "$RAPL_PATH")
    START_TIME=$(date +%s%N)

    TURBOSTAT_OUT=$(mktemp)
    # turbostat measures exactly the wrapped command's execution window.
    # -q suppresses the config dump; -S prints one system-summary row
    # instead of one row per core.
    turbostat -q -S --show PkgWatt,RAMWatt -- bash -c "$COMMAND" 2> "$TURBOSTAT_OUT" >/dev/null
    WORKLOAD_EXIT=$?

    END_TIME=$(date +%s%N)
    ENERGY_AFTER=$(cat "$RAPL_PATH")

    DURATION_NS=$((END_TIME - START_TIME))
    DURATION_S=$(echo "scale=3; $DURATION_NS / 1000000000" | bc)

    if (( ENERGY_AFTER < ENERGY_BEFORE )); then
        ENERGY_UJ=$((ENERGY_MAX - ENERGY_BEFORE + ENERGY_AFTER))
    else
        ENERGY_UJ=$((ENERGY_AFTER - ENERGY_BEFORE))
    fi
    RAPL_ENERGY_J=$(echo "scale=6; $ENERGY_UJ / 1000000" | bc)

    # Parse turbostat's header + summary row for PkgWatt / RAMWatt columns
    HEADER_LINE=$(grep -m1 'PkgWatt' "$TURBOSTAT_OUT")
    VALUES_LINE=$(tail -n1 "$TURBOSTAT_OUT")
    read -r -a HEADER <<< "$HEADER_LINE"
    read -r -a VALUES <<< "$VALUES_LINE"

    PKG_WATT=0
    RAM_WATT=0
    for i in "${!HEADER[@]}"; do
        case "${HEADER[$i]}" in
            PkgWatt) PKG_WATT="${VALUES[$i]:-0}" ;;
            RAMWatt) RAM_WATT="${VALUES[$i]:-0}" ;;
        esac
    done

    TURBOSTAT_PKG_J=$(echo "scale=6; $PKG_WATT * $DURATION_S" | bc)
    TURBOSTAT_RAM_J=$(echo "scale=6; $RAM_WATT * $DURATION_S" | bc)

    DELTA_PCT=$(awk -v r="$RAPL_ENERGY_J" -v t="$TURBOSTAT_PKG_J" \
        'BEGIN { if (r+0 == 0) print "NA"; else printf "%.2f", ((r - t) / r) * 100 }')

    TIMESTAMP=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    echo "${rep},${TASK_NAME},${DURATION_S},${RAPL_ENERGY_J},${TURBOSTAT_PKG_J},${TURBOSTAT_RAM_J},${DELTA_PCT},${TIMESTAMP}" >> "$OUT_FILE"

    echo "[rep ${rep}/${REPS}] duration=${DURATION_S}s  RAPL=${RAPL_ENERGY_J}J  turbostat_pkg=${TURBOSTAT_PKG_J}J  delta=${DELTA_PCT}%"

    if [[ "$DELTA_PCT" != "NA" ]]; then
        if awk -v d="$DELTA_PCT" -v t="$TOLERANCE_PCT" 'BEGIN { d = (d < 0) ? -d : d; exit !(d > t) }'; then
            echo "  WARNING: discrepancy exceeds ${TOLERANCE_PCT}% tolerance" >&2
        fi
    fi

    if [[ $WORKLOAD_EXIT -ne 0 ]]; then
        echo "  WARNING: workload exited with code ${WORKLOAD_EXIT}" >&2
    fi

    rm -f "$TURBOSTAT_OUT"
done

# Summary across this run's reps (the REPS lines just appended)
tail -n "$REPS" "$OUT_FILE" | awk -F, -v task="$TASK_NAME" -v tol="$TOLERANCE_PCT" '
    $7 != "NA" {
        d = $7 + 0
        if (d < 0) d = -d
        sum += d
        count++
        if (d > max) max = d
    }
    END {
        if (count > 0) {
            printf "\nSummary (%d rep(s), task=%s): mean |delta|=%.2f%%  max |delta|=%.2f%%  tolerance=%d%%\n", count, task, sum/count, max, tol
            if (sum/count > tol) print "RESULT: mean discrepancy exceeds tolerance — investigate before trusting RAPL data."
            else print "RESULT: RAPL and turbostat agree within tolerance."
        }
    }
'

echo "Full results appended to ${OUT_FILE}"
