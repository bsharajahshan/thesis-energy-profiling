#!/bin/bash

# diagnose_work.sh - RQ2/RQ3 diagnostic side-run: "what work is actually being done?"
#
# Added 2026-10-13 after the call with Philipp. Question that triggered it:
# why does cpu_ram parallel show LESS DRAM energy (1.05 W) than ram_io
# (1.36 W) and than RAM alone (1.27 W)? Working hypotheses (untested):
#   H1 scheduling/SMT - `--cpu 8` fills all 8 logical CPUs, the single
#      `--vm` worker gets less CPU time, so it makes fewer memory accesses.
#   H2 power limit    - package sits near its power limit, cores slow down,
#      the memory loop slows with them.
# Philipp's reply (2026-10-15) added two things: (a) log how much data actually
# moves to/from DRAM (needed for the mixed tests, to see whether the amount of
# data is the same as in the isolated runs), and (b) test partial CPU load
# (--cpu-load) because stress-ng at 100% never stalls, so a second hardware
# thread on the same core has nothing to fill in. Cases cpu4_alone, cpu_alone
# and cpu8_half below show directly whether 8 threads do more total work than
# 4 threads (hyperthreading gain) and what 8 threads at 50% load do.
#
# This script does NOT replace or modify the main dataset. It re-runs a few
# selected cases through the SAME measure_rapl.sh wrapper (so energy numbers
# are comparable) and additionally records what the workers actually did.
#
# Low-overhead rule: nothing here samples inside the measurement window
# except turbostat at a 5 s interval. Work counters come from the tools
# themselves (stress-ng --metrics-brief and fio JSON print totals once at
# the end). The "nolog" control cases run the identical workload without any
# of the logging, so the overhead of the logging can be measured instead of
# assumed (diagnostics_summary.py compares them).
#
# Usage (needs root for RAPL/turbostat; sudo does not see user-local PATH
# entries such as ~/.local/bin/fio, so pass PATH through):
#   sudo env "PATH=$PATH" bash scripts/diagnose_work.sh [duration_s=60] [reps=5] [out_dir=results/diagnostics]
# Optional environment switches:
#   USE_PERF=0      disable perf (default: on if installed). perf counts in
#                   hardware and is read once at the end: instructions, cycles,
#                   context switches, migrations and - if the machine exposes
#                   them - uncore_imc data_reads/data_writes = MiB moved
#                   to/from DRAM (system-wide, which is what the mixed tests need)
#   USE_TURBOSTAT=0 disable the turbostat background logger (default: on if installed)
#   COOLDOWN=20     seconds to wait between runs (default 0, like the main runs)
#   MEASURE_SCRIPT  path to the RAPL wrapper (default: measure_rapl.sh next to this file)
#
# Output (all under out_dir, gitignored via results/):
#   diagnostics.csv  one row per run: energy, DRAM, CPU util, avg Bzy_MHz, max PkgTmp
#   work_log.csv     one row per worker per run: bogo-ops, CPU time, CPU share %
#   perf_log.csv     one row per perf event per run (when perf is installed and not disabled)
#   raw/<case>_<rep>/  untouched tool outputs for every run
#
# Case order is shuffled within every repetition so slow drift (temperature,
# background activity) does not line up with one case.

set -u
export LC_ALL=C

DURATION="${1:-60}"
REPS="${2:-5}"
OUT_DIR="${3:-results/diagnostics}"
COOLDOWN="${COOLDOWN:-0}"
USE_PERF="${USE_PERF:-1}"
USE_TURBOSTAT="${USE_TURBOSTAT:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEASURE="${MEASURE_SCRIPT:-${SCRIPT_DIR}/measure_rapl.sh}"

# case name | logging (1/0) | components. Same constituent commands as RQ1/RQ2
# (cpu8 = stress-ng --cpu 8, vm = --vm 1 --vm-bytes 8G, io = fio write 4k),
# plus cpu4 (4 CPU threads, leaves logical CPUs free) to separate H1 from H2.
CASES=(
    "ram_alone|1|vm"
    "cpu_alone|1|cpu8"
    "cpu4_alone|1|cpu4"
    "cpu8_half|1|cpu8h"
    "cpu_ram|1|cpu8 vm"
    "cpu4_ram|1|cpu4 vm"
    "ram_io|1|vm io"
    "ram_alone_nolog|0|vm"
    "cpu_ram_nolog|0|cpu8 vm"
)

for tool in stress-ng bc awk; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool not found in PATH" >&2; exit 1; }
done
command -v fio >/dev/null 2>&1 || { echo "ERROR: fio not found in PATH (sudo drops user-local PATH; use: sudo env \"PATH=\$PATH\" bash $0)" >&2; exit 1; }
[[ -f "$MEASURE" ]] || { echo "ERROR: RAPL wrapper not found: $MEASURE" >&2; exit 1; }

TURBOSTAT_ON=0
if [[ "$USE_TURBOSTAT" == "1" ]] && command -v turbostat >/dev/null 2>&1; then TURBOSTAT_ON=1; fi
PERF_ON=0
PERF_EVENTS="instructions,cycles,context-switches,cpu-migrations"
if [[ "$USE_PERF" == "1" ]]; then
    if command -v perf >/dev/null 2>&1; then
        PERF_ON=1
        # DRAM bandwidth counters exist only on some machines/kernels
        if perf list 2>/dev/null | grep -q "uncore_imc/data_reads"; then
            PERF_EVENTS="${PERF_EVENTS},uncore_imc/data_reads/,uncore_imc/data_writes/"
        else
            echo "WARNING: no uncore_imc DRAM counters in perf list - DRAM traffic cannot be measured directly." >&2
            echo "         Fallback: estimate GB touched from vm bogo-ops x chunk size (see work_log.csv)." >&2
        fi
    else
        echo "WARNING: perf is not installed - no DRAM traffic counters, continuing without it" >&2
    fi
fi

mkdir -p "${OUT_DIR}/raw"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
SCRATCH="$(mktemp -d "${OUT_DIR}/scratch.XXXXXX")"
trap 'rm -rf "$SCRATCH"; [[ -n "${TS_PID:-}" ]] && kill "$TS_PID" 2>/dev/null' EXIT

DIAG_CSV="${OUT_DIR}/diagnostics.csv"
WORK_CSV="${OUT_DIR}/work_log.csv"
PERF_CSV="${OUT_DIR}/perf_log.csv"
[[ -f "$DIAG_CSV" ]] || echo "case,logging,run_id,duration_s,energy_uj,energy_j,dram_energy_uj,dram_energy_j,cpu_util_pct,avg_bzy_mhz,max_pkg_temp_c,avg_pkg_watt_turbostat,timestamp" > "$DIAG_CSV"
[[ -f "$WORK_CSV" ]] || echo "case,logging,run_id,component,workers,bogo_ops,real_s,usr_s,sys_s,bogo_ops_per_s,cpu_share_pct" > "$WORK_CSV"
if [[ $PERF_ON -eq 1 ]] && [[ ! -f "$PERF_CSV" ]]; then echo "case,logging,run_id,event,value,unit" > "$PERF_CSV"; fi

echo "diagnose_work: duration=${DURATION}s reps=${REPS} out=${OUT_DIR}"
echo "  turbostat logging: $([[ $TURBOSTAT_ON -eq 1 ]] && echo on || echo off)   perf: $([[ $PERF_ON -eq 1 ]] && echo "on (${PERF_EVENTS})" || echo off)"

# ---- command building -------------------------------------------------------

# $1 = component, $2 = logging (1/0), $3 = run dir. Prints one shell command.
component_cmd() {
    local comp="$1" log="$2" run_dir="$3" cmd out
    case "$comp" in
        cpu8) cmd="stress-ng --cpu 8 --timeout ${DURATION}s" ;;
        cpu4) cmd="stress-ng --cpu 4 --timeout ${DURATION}s" ;;
        cpu8h) cmd="stress-ng --cpu 8 --cpu-load 50 --timeout ${DURATION}s" ;;
        vm)   cmd="stress-ng --vm 1 --vm-bytes 8G --timeout ${DURATION}s" ;;
        io)   cmd="fio --ioengine=libaio --iodepth=16 --blocksize=4k --name=io_job --rw=write --size=1G --runtime=${DURATION} --time_based" ;;
        *) echo "unknown component $comp" >&2; return 1 ;;
    esac
    if [[ "$log" == "1" ]]; then
        if [[ "$comp" == "io" ]]; then
            cmd="${cmd} --output-format=json --output=${run_dir}/io.json"
        else
            cmd="${cmd} --metrics-brief"
        fi
        out="${run_dir}/${comp}.log"
    else
        out="/dev/null"
    fi
    echo "${cmd} > ${out} 2>&1"
}

# $1 = components (space separated), $2 = logging, $3 = run dir. Prints the full command.
build_command() {
    local comps=($1) log="$2" run_dir="$3" c cmds=()
    for c in "${comps[@]}"; do cmds+=("$(component_cmd "$c" "$log" "$run_dir")"); done
    if [[ ${#cmds[@]} -eq 1 ]]; then
        echo "( ${cmds[0]} )"
    else
        # Same pattern as mixed_load.nf: outer subshell so the bare `wait`
        # only waits on the workloads, not on measure_rapl.sh's own sampler.
        local s="("
        for c in "${cmds[@]}"; do s="${s} (${c}) &"; done
        echo "${s} wait )"
    fi
}

# ---- parsers ----------------------------------------------------------------

# stress-ng summary line: "... ] <name> <bogo> <real_s> <usr_s> <sys_s> <ops/s real> ..."
# (same shape the existing verify_work_equivalence.sh parser relies on).
# CPU share = (usr+sys) / (real * workers): how much of one logical CPU each
# worker actually got. Prints: bogo,real,usr,sys,ops_per_s,share
parse_stress_log() {
    awk -v name="$2" -v workers="$3" '
        $0 ~ "\\] +" name " +[0-9]" {
            sub(/^.*\] +/, "")
            share = ""
            if ($3 > 0 && workers > 0) share = sprintf("%.1f", 100 * ($4 + $5) / ($3 * workers))
            printf "%s,%s,%s,%s,%s,%s\n", $2, $3, $4, $5, $6, share
            found = 1; exit
        }
        END { if (!found) print ",,,,," }' "$1"
}

# fio JSON: total bytes written and IOPS, and fio's own usr/sys CPU %. Best effort,
# plain awk (no jq dependency). Prints: bytes,real_s,usr_cpu_pct,sys_cpu_pct,iops
parse_fio_json() {
    awk -F: '
        /"usr_cpu"/ { gsub(/[^0-9.]/, "", $2); usr = $2 }
        /"sys_cpu"/ { gsub(/[^0-9.]/, "", $2); sys = $2 }
        /"write"[ ]*:/ { w = 1 }
        w && /"io_bytes"/ && !b { gsub(/[^0-9.]/, "", $2); bytes = $2; b = 1 }
        w && /"iops"/ && !i { gsub(/[^0-9.]/, "", $2); iops = $2; i = 1 }
        w && /"runtime"/ && !r { gsub(/[^0-9.]/, "", $2); rt = $2; r = 1 }
        END { printf "%s,%s,%s,%s,%s\n", bytes, rt, usr, sys, iops }' "$1" 2>/dev/null
}

# turbostat --Summary log: mean Bzy_MHz / mean PkgWatt, max PkgTmp, skipping the
# first interval (it overlaps turbostat start-up). Prints: avg_mhz,max_temp,avg_watt
parse_turbostat() {
    awk '
        /Bzy_MHz/ && !hdr { for (i = 1; i <= NF; i++) col[$i] = i; hdr = 1; next }
        hdr && $1 ~ /^[0-9.]+$/ {
            n++; if (n == 1) next
            if (col["Bzy_MHz"]) { mhz += $(col["Bzy_MHz"]); m++ }
            if (col["PkgWatt"]) { w += $(col["PkgWatt"]); k++ }
            if (col["PkgTmp"] && $(col["PkgTmp"]) + 0 > tmax) tmax = $(col["PkgTmp"]) + 0
        }
        END { printf "%s,%s,%s\n", (m ? sprintf("%.0f", mhz / m) : ""), (tmax ? tmax : ""), (k ? sprintf("%.2f", w / k) : "") }' "$1" 2>/dev/null
}

# ---- one run ----------------------------------------------------------------

run_case() {
    local entry="$1" rep="$2"
    local name="${entry%%|*}" rest="${entry#*|}"
    local log="${rest%%|*}" comps="${rest#*|}"
    local run_dir="${OUT_DIR}/raw/${name}_${rep}"
    mkdir -p "$run_dir"

    local cmd
    cmd="$(build_command "$comps" "$log" "$run_dir")"
    if [[ "$log" == "1" && $PERF_ON -eq 1 ]]; then
        cmd="perf stat -a -x, -o '${run_dir}/perf.csv' -e ${PERF_EVENTS} -- bash -c '${cmd}'"
    fi

    TS_PID=""
    if [[ "$log" == "1" && $TURBOSTAT_ON -eq 1 ]]; then
        turbostat --quiet --Summary --interval 5 --show Bzy_MHz,PkgTmp,PkgWatt --out "${run_dir}/turbostat.log" >/dev/null 2>&1 &
        TS_PID=$!
        sleep 2   # let turbostat finish its start-up before the RAPL window opens
    fi

    local timestamp out
    timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    out="$( cd "$SCRATCH" && bash "$MEASURE" "$cmd" "diag_${name}" )"
    echo "$out" > "${run_dir}/rapl.txt"

    if [[ -n "$TS_PID" ]]; then
        kill "$TS_PID" 2>/dev/null; wait "$TS_PID" 2>/dev/null; TS_PID=""
    fi
    rm -rf "${SCRATCH:?}"/* 2>/dev/null   # fio's 1 GB test file

    local d eu ej du dj cu
    d=$(echo "$out" | grep "^DURATION_S=" | cut -d= -f2)
    eu=$(echo "$out" | grep "^ENERGY_UJ=" | cut -d= -f2)
    ej=$(echo "$out" | grep "^ENERGY_J=" | cut -d= -f2)
    du=$(echo "$out" | grep "^DRAM_ENERGY_UJ=" | cut -d= -f2)
    dj=$(echo "$out" | grep "^DRAM_ENERGY_J=" | cut -d= -f2)
    cu=$(echo "$out" | grep "^CPU_UTIL_PCT=" | cut -d= -f2)

    local ts_vals=",,"
    if [[ "$log" == "1" && $TURBOSTAT_ON -eq 1 && -s "${run_dir}/turbostat.log" ]]; then
        ts_vals="$(parse_turbostat "${run_dir}/turbostat.log")"
    fi
    echo "${name},${log},${rep},${d},${eu},${ej},${du},${dj},${cu},${ts_vals},${timestamp}" >> "$DIAG_CSV"

    if [[ "$log" == "1" ]]; then
        local comp stressor workers
        for comp in $comps; do
            case "$comp" in
                cpu8) stressor=cpu; workers=8 ;;
                cpu4) stressor=cpu; workers=4 ;;
                cpu8h) stressor=cpu; workers=8 ;;
                vm)   stressor=vm;  workers=1 ;;
                io)   stressor=io;  workers=1 ;;
            esac
            if [[ "$comp" == "io" ]]; then
                # fio: bytes,runtime_ms,usr%,sys%,iops -> same columns where they fit
                local f; f="$(parse_fio_json "${run_dir}/io.json")"
                IFS=, read -r f_bytes f_rt f_usr f_sys f_iops <<< "$f"
                echo "${name},${log},${rep},io_fio,${workers},${f_bytes},${f_rt},${f_usr},${f_sys},${f_iops}," >> "$WORK_CSV"
            else
                echo "${name},${log},${rep},${comp},${workers},$(parse_stress_log "${run_dir}/${comp}.log" "$stressor" "$workers")" >> "$WORK_CSV"
            fi
        done
        if [[ $PERF_ON -eq 1 && -s "${run_dir}/perf.csv" ]]; then
            # perf stat -x, lines: value,unit,event,...  (skip comments/not-counted)
            awk -F, -v c="$name" -v l="$log" -v r="$rep" '
                $1 ~ /^[0-9.]+$/ { printf "%s,%s,%s,%s,%s,%s\n", c, l, r, $3, $1, $2 }' "${run_dir}/perf.csv" >> "$PERF_CSV"
        fi
    fi

    echo "  ${name} rep ${rep}: ${d}s  pkg ${ej} J  dram ${dj} J  cpu ${cu}%"
}

# ---- main loop --------------------------------------------------------------

for rep in $(seq 1 "$REPS"); do
    echo "== repetition ${rep}/${REPS} =="
    # Read the shuffled order into an array first: a `while read` loop fed by
    # a pipe would share stdin with the workloads, and any child that reads
    # stdin (perf, fio, ...) would silently eat the remaining cases.
    mapfile -t ORDER < <(printf '%s\n' "${CASES[@]}" | shuf)
    for entry in "${ORDER[@]}"; do
        run_case "$entry" "$rep" < /dev/null
        (( COOLDOWN > 0 )) && sleep "$COOLDOWN"
    done
done

echo "Done. Results in ${OUT_DIR} (diagnostics.csv, work_log.csv, raw/)."
echo "Next: py scripts/analysis/diagnostics_summary.py ${OUT_DIR}"
