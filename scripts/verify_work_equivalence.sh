#!/bin/bash

# verify_work_equivalence.sh - RQ3 work-equivalence sanity check
#
# Confirms the premise the RQ3 granularity sweep depends on: splitting a
# fixed total resource (8 CPU threads / 8GB RAM) into N sub-tasks of (8/N)
# each, run for the same duration, produces equivalent total completed work
# regardless of N. Without this, RQ3 has no scientific validity (CLAUDE.md,
# Key Scientific Constraints).
#
# Uses stress-ng's own --metrics-brief bogo-ops counter as ground truth
# rather than assuming linear scaling with thread/byte count. Each
# subsystem x N x mode combination is run in the SAME execution pattern as
# the real granularity.nf processes (parallel = concurrent sub-tasks,
# sequential = back-to-back) rather than a nominal/sequential-only proxy —
# that distinction matters for RAM specifically, since parallel RAM workers
# are deliberately not bandwidth-capped (Philipp's confirmed choice) and may
# show real contention between sub-tasks that a sequential-only check would
# miss entirely.
#
# This is a pre-flight check, not part of the CSV pipeline (RQ3's schema
# has no additional columns, per CLAUDE.md). Run once manually before the
# main sweep. No root/RAPL access required — deliberately independent of
# measure_rapl.sh and the concurrency-serialization fix in nextflow.config,
# since this runs standalone via bash, not through Nextflow.

set -u
export LC_ALL=C

DURATION="${1:-30}"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# $1 = output file, $2 = stressor name as stress-ng reports it (cpu | vm).
# Matches "] <name> <digits>" — the shape stress-ng uses for both the
# --metrics-brief ("info:") and --metrics ("metrc:") tags across versions —
# and takes the field right after the name, which is the raw bogo-ops count
# (not the bogo-ops/s rate columns that follow it).
extract_bogo_ops() {
    awk -v name="$2" '
        $0 ~ "\\] +" name " +[0-9]" {
            sub(/^.*\] +/, "")
            print $2
            exit
        }' "$1"
}

# $1 = label, $2 = stressor name, $3 = total resource units (threads or GB)
run_subsystem() {
    local label="$1" stressor="$2" total_units="$3"

    for mode in sequential parallel; do
        echo "=== ${label} work equivalence — ${mode} (duration=${DURATION}s) ==="
        printf "%-4s %-12s %-16s %-16s\n" "N" "units/task" "bogo-ops/task" "total-bogo-ops"
        for N in 1 2 4 8; do
            local units=$((total_units / N))
            local total_ops=0
            local pids=()

            for i in $(seq 1 "$N"); do
                local out_file="${TMP_DIR}/${label}_${mode}_${N}_${i}.out"
                local cmd
                if [[ "$stressor" == "cpu" ]]; then
                    cmd=(stress-ng --cpu "$units" --timeout "${DURATION}s" --metrics-brief)
                else
                    cmd=(stress-ng --vm 1 --vm-bytes "${units}G" --timeout "${DURATION}s" --metrics-brief)
                fi

                if [[ "$mode" == "parallel" ]]; then
                    "${cmd[@]}" > "$out_file" 2>&1 &
                    pids+=("$!")
                else
                    "${cmd[@]}" > "$out_file" 2>&1
                fi
            done

            if [[ "$mode" == "parallel" ]]; then
                for pid in "${pids[@]}"; do wait "$pid"; done
            fi

            for i in $(seq 1 "$N"); do
                local out_file="${TMP_DIR}/${label}_${mode}_${N}_${i}.out"
                local ops
                ops=$(extract_bogo_ops "$out_file" "$stressor")
                if [[ -z "$ops" ]]; then
                    echo "WARNING: could not parse bogo-ops for ${label} ${mode} N=$N task $i — raw output:" >&2
                    cat "$out_file" >&2
                    ops=0
                fi
                total_ops=$((total_ops + ops))
            done

            printf "%-4s %-12s %-16s %-16s\n" "$N" "$units" "$((total_ops / N))" "$total_ops"
        done
        echo
    done
}

run_subsystem "cpu" "cpu" 8
run_subsystem "ram" "vm" 8

echo "Compare 'total-bogo-ops' across N within each table (same subsystem,"
echo "same mode) - values should be roughly constant, allowing for stress-ng"
echo "startup overhead and scheduling noise."
echo ""
echo "Expected: CPU roughly flat in both modes (concurrent thread count"
echo "stays ~8 regardless of split). RAM roughly flat in sequential but may"
echo "decline with N in parallel - that decline, if present, IS the"
echo "memory-bandwidth-contention effect Philipp asked to be reported as a"
echo "finding, not a bug (CLAUDE.md: RAM non-comparability in RQ3)."
echo ""
echo "Large unexplained drift on the CPU side, or in the RAM sequential row,"
echo "would instead indicate the work-equivalence premise itself is broken"
echo "and RQ3 needs revisiting before trusting results."
