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
# RAM-specific correction (added 2026-09-19, after the first real RQ3 run
# surfaced this): raw bogo-ops are NOT directly comparable across N for the
# `vm` stressor, unlike `cpu`. One vm bogo-op is one pass over THIS worker's
# own --vm-bytes chunk — and that chunk shrinks as N grows (8/N GB per
# worker) — so a worker with a smaller chunk completes proportionally more
# "laps" in the same time even if its real memory-touching rate (bytes/sec)
# never changed. Summing raw bogo-ops across workers therefore inflates
# automatically with N regardless of whether real total work changed at
# all — confirmed against real data, where RAM's sequential-mode total
# (zero possible contention, since only one worker runs at a time) still
# exploded ~85x from N=1 to N=8. The fix: convert bogo-ops to GB actually
# touched (bogo_ops * chunk_size_gb) before comparing — see `total_gb`
# below. `total-bogo-ops` is kept in the RAM table for reference/debugging
# only; it is not a valid equivalence check on its own. The `cpu` stressor
# does not have this problem (one cpu bogo-op is a fixed-size unit of work
# regardless of thread count), so its table and comparison are unchanged.
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
        if [[ "$stressor" == "vm" ]]; then
            printf "%-4s %-12s %-16s %-18s %-18s\n" "N" "units/task" "bogo-ops/task" "total-bogo-ops*" "total-GB-touched"
        else
            printf "%-4s %-12s %-16s %-16s\n" "N" "units/task" "bogo-ops/task" "total-bogo-ops"
        fi
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

            if [[ "$stressor" == "vm" ]]; then
                # units is the same for every worker in this N-group (each
                # gets total_units/N), so total_ops * units = sum of each
                # worker's (ops * its own chunk size) — the GB actually
                # touched, a unit that means the same amount of real work
                # regardless of how the total was split. See header comment.
                local total_gb=$((total_ops * units))
                printf "%-4s %-12s %-16s %-18s %-18s\n" "$N" "$units" "$((total_ops / N))" "$total_ops" "$total_gb"
            else
                printf "%-4s %-12s %-16s %-16s\n" "$N" "$units" "$((total_ops / N))" "$total_ops"
            fi
        done
        echo
    done
}

run_subsystem "cpu" "cpu" 8
run_subsystem "ram" "vm" 8

echo "CPU: compare 'total-bogo-ops' across N - values should be roughly"
echo "constant in both modes (concurrent thread count stays ~8 regardless of"
echo "split, and one cpu bogo-op is a fixed-size unit of work no matter how"
echo "many workers you split into, so the raw count is directly comparable)."
echo "Large unexplained drift here would mean the work-equivalence premise"
echo "itself is broken and RQ3 needs revisiting before trusting results."
echo ""
echo "RAM: use 'total-GB-touched', NOT 'total-bogo-ops*'. One vm bogo-op is"
echo "one pass over a worker's OWN --vm-bytes chunk, and that chunk shrinks"
echo "as N grows - so raw bogo-ops inflate automatically with N even when"
echo "real work hasn't changed at all (see header comment). The '*' column"
echo "is kept only for reference/debugging."
echo ""
echo "Don't expect total-GB-touched to be flat across N in either mode -"
echo "more total workers doing more total work is normal and expected, in"
echo "both sequential and parallel. What actually indicates contention is"
echo "comparing PARALLEL against SEQUENTIAL at the SAME N: sequential"
echo "workers never run at the same time, so they never compete for memory"
echo "bandwidth - its total-GB-touched at a given N is the 'no contention'"
echo "reference point for what N workers' combined output should look like."
echo "If parallel's total-GB-touched at that N falls noticeably short of"
echo "sequential's, that gap is the real memory-bandwidth-contention effect"
echo "Philipp asked to be reported as a finding (CLAUDE.md: RAM"
echo "non-comparability in RQ3) - not a bug in this script."
