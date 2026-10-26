#!/bin/bash
# run_case.sh - runs ONE measured case (one or more RAPL windows) and writes
#   <out_dir>/<case_id>.csv        one row per RAPL window, with a header row
#   <out_dir>/raw_<case_id>/       raw tool output (stress-ng logs, fio json, perf csv, window scripts)
# Fixed WORK per job (not fixed time); the amount of work comes from calibration.env.
#
# Usage:
#   run_case.sh <out_dir> <case_id> <experiment> <mode> <label> <intensity> <task_count> <cpu_load> <rep> <run_index> -- <component>...
#
#   mode      single      one component, one window
#             parallel    all components start together, one window
#             sequential  one window per component, one after the other
#   component "type:key=value,key=value"
#     cpu:threads=8,div=1[,cpus=0-3][,time=60]   stress-ng --cpu. div = number of sub-tasks the job
#                                                is split into (ops = CPU_OPS_TOTAL / div).
#                                                time=S: fixed time S seconds instead of fixed work.
#     ram:chunk_gb=8,div=1[,cpus=3][,time=60]    stress-ng --vm 1 --vm-bytes <chunk_gb>G.
#                                                ops = VM_TARGET_GIB / div * VM_OPS_PER_GIB_C<chunk_gb>.
#     io:rw=write|randwrite                      fio, IO_TOTAL_GIB written (buffered, fsync at the end).
#     idle:secs=60                               sleep
#   cpu_load (argument 8): 100 = full load; below 100 adds --cpu-load to every cpu component.
#
# Environment: CALIBRATION (default: scripts/calibration.env), CASE_PERF=off (no perf in this case),
#   NO_LOCK=1 (tests only).
# Always exits 0 after the measurement, even if a workload failed (exit_code column, work_complete
# column); a missing calibration or bad arguments stop it with exit 2.

set -u
export LC_ALL=C
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shlib/parsers.sh"
source "$SCRIPT_DIR/shlib/sensors.sh"

if [[ $# -lt 12 ]]; then
    echo "usage: run_case.sh <out_dir> <case_id> <experiment> <mode> <label> <intensity> <task_count> <cpu_load> <rep> <run_index> -- <component>..." >&2
    exit 2
fi
OUT_DIR="$1"; CASE_ID="$2"; EXPERIMENT="$3"; MODE="$4"; LABEL="$5"; INTENSITY="$6"
TASK_COUNT="$7"; CPU_LOAD="$8"; REP="$9"; RUN_INDEX="${10}"
shift 10
[[ "$1" == "--" ]] && shift
COMPS=("$@")
case "$MODE" in single|parallel|sequential) ;; *) echo "bad mode: $MODE" >&2; exit 2 ;; esac
[[ ${#COMPS[@]} -ge 1 ]] || { echo "no component given" >&2; exit 2; }

CALIBRATION="${CALIBRATION:-$SCRIPT_DIR/calibration.env}"
[[ -f "$CALIBRATION" ]] || { echo "ERROR: calibration file not found: $CALIBRATION (see calibration.env.template)" >&2; exit 2; }
# shellcheck disable=SC1090
source "$CALIBRATION"
CPU_OPS_SEMANTICS="${CPU_OPS_SEMANTICS:-total}"
WATCHDOG_S="${WATCHDOG_S:-1200}"
IO_BS="${IO_BS:-4k}"; IO_DEPTH="${IO_DEPTH:-16}"; IO_TOTAL_GIB="${IO_TOTAL_GIB:-8}"
PERF_ON="${PERF_ON:-0}"
[[ "${CASE_PERF:-on}" == "off" ]] && PERF_ON=0

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
RAW="$OUT_DIR/raw_${CASE_ID}"
mkdir -p "$RAW"
CSV="$OUT_DIR/${CASE_ID}.csv"
MEASURE="${MEASURE_SCRIPT:-$SCRIPT_DIR/measure_rapl.sh}"

# One case at a time on the whole machine (also protects the set-up steps, e.g. fio file creation).
if [[ -z "${NO_LOCK:-}" ]]; then
    if command -v flock >/dev/null 2>&1; then
        exec 8>"${RAPL_LOCK_FILE:-/tmp/thesis_rapl_measure.lock}"
        flock 8
    else
        echo "WARNING: flock not found - cases are not protected against overlap" >&2
    fi
fi
export RAPL_LOCK_HELD=1

# ---- build the components ---------------------------------------------------
declare -a C_TYPE C_CMD C_LOG C_REQ C_CHUNK
rnd() { awk -v x="$1" 'BEGIN { printf "%d", x + 0.5 }'; }

build_component() {
    local i="$1" spec="$2" type cpus prefix=""
    type="$(comp_type "$spec")"
    C_TYPE[$i]="$type"; C_REQ[$i]=""; C_CHUNK[$i]=""
    cpus="$(comp_get "$spec" cpus "")"
    cpus="${cpus//;/,}"      # in a component spec the list is written 0-2;4-6 (commas separate the keys)
    [[ -n "$cpus" ]] && prefix="taskset -c $cpus "
    case "$type" in
        cpu)
            local threads div time ops load_arg="" opts
            threads="$(comp_get "$spec" threads 8)"; div="$(comp_get "$spec" div 1)"; time="$(comp_get "$spec" time "")"
            [[ "$CPU_LOAD" -lt 100 ]] && load_arg="--cpu-load $CPU_LOAD"
            if [[ -n "$time" ]]; then
                opts="--timeout ${time}s"
            else
                ops="$(rnd "$(awk -v t="$CPU_OPS_TOTAL" -v d="$div" 'BEGIN { print t / d }')")"
                C_REQ[$i]="$ops"
                [[ "$CPU_OPS_SEMANTICS" == "per_worker" ]] && ops="$(rnd "$(awk -v o="$ops" -v t="$threads" 'BEGIN { print o / t }')")"
                opts="--cpu-ops $ops --timeout ${WATCHDOG_S}s"
            fi
            C_LOG[$i]="$RAW/${CASE_ID}_c${i}_cpu.log"
            C_CMD[$i]="${prefix}stress-ng --cpu $threads --cpu-method $CPU_METHOD $load_arg $opts --metrics-brief"
            ;;
        ram)
            local chunk div time var opg ops opts
            chunk="$(comp_get "$spec" chunk_gb 8)"; div="$(comp_get "$spec" div 1)"; time="$(comp_get "$spec" time "")"
            C_CHUNK[$i]="$chunk"
            if [[ -n "$time" ]]; then
                opts="--timeout ${time}s"
            else
                var="VM_OPS_PER_GIB_C${chunk}"; opg="${!var:-}"
                [[ -n "$opg" ]] || { echo "ERROR: $var missing in $CALIBRATION" >&2; exit 2; }
                ops="$(rnd "$(awk -v t="$VM_TARGET_GIB" -v d="$div" -v o="$opg" 'BEGIN { print t / d * o }')")"
                C_REQ[$i]="$ops"
                opts="--vm-ops $ops --timeout ${WATCHDOG_S}s"
            fi
            C_LOG[$i]="$RAW/${CASE_ID}_c${i}_vm.log"
            C_CMD[$i]="${prefix}stress-ng --vm 1 --vm-bytes ${chunk}G --vm-method $VM_METHOD $opts --metrics-brief"
            ;;
        io)
            local rw; rw="$(comp_get "$spec" rw write)"
            C_LOG[$i]="$RAW/${CASE_ID}_c${i}_io.json"
            C_CMD[$i]="${prefix}fio --ioengine=libaio --iodepth=$IO_DEPTH --blocksize=$IO_BS --name=io_job --rw=$rw --size=1G --io_size=${IO_TOTAL_GIB}G --end_fsync=1 --output-format=json --output=${C_LOG[$i]}"
            C_REQ[$i]="$(rnd "$(awk -v g="$IO_TOTAL_GIB" 'BEGIN { print g * 1073741824 }')")"
            ;;
        idle)
            C_LOG[$i]="$RAW/${CASE_ID}_c${i}_idle.log"
            C_CMD[$i]="sleep $(comp_get "$spec" secs 60)"
            ;;
        *) echo "unknown component type: $type" >&2; exit 2 ;;
    esac
}

for i in "${!COMPS[@]}"; do build_component "$i" "${COMPS[$i]}"; done

# ---- windows ----------------------------------------------------------------
declare -a WINDOWS   # each entry: space separated component indexes
if [[ "$MODE" == "sequential" ]]; then
    for i in "${!COMPS[@]}"; do WINDOWS+=("$i"); done
else
    WINDOWS+=("${!COMPS[*]}")
fi

HEADER="case_id,experiment,mode,label,intensity,task_count,cpu_load,rep,run_index,window,window_name,components,timestamp,start_ns,end_ns,duration_s,energy_uj,energy_j,dram_energy_uj,dram_energy_j,cpu_util_pct,mem_util_pct,cpu_ops,cpu_ops_req,vm_ops,vm_ops_req,vm_gib_est,io_bytes,io_iops,work_complete,instructions,cycles,dram_read_mib,dram_write_mib,temp_before_c,temp_after_c,throttle_pkg_delta,throttle_core_delta,pl_long_w,pl_short_w,exit_code"
echo "$HEADER" > "$CSV"

delta() { if [[ "$1" =~ ^[0-9]+$ && "$2" =~ ^[0-9]+$ ]]; then echo $(( $2 - $1 )); else echo NA; fi; }

w=0
for win in "${WINDOWS[@]}"; do
    w=$((w + 1))
    idx=($win)
    names=""; allidle=1; has_io=0; script="$RAW/${CASE_ID}_w${w}.sh"
    {
        echo "#!/bin/bash"
        n=0
        for i in "${idx[@]}"; do
            n=$((n + 1))
            # fio writes its json itself (--output), so its console output goes to a separate file
            if [[ "${C_TYPE[$i]}" == "io" ]]; then
                echo "${C_CMD[$i]} > '${C_LOG[$i]}.out' 2>&1 &"
            else
                echo "${C_CMD[$i]} > '${C_LOG[$i]}' 2>&1 &"
            fi
            echo "pid$n=\$!"
        done
        echo "rc=0"
        for k in $(seq 1 $n); do echo "wait \$pid$k || rc=1"; done
        echo "exit \$rc"
    } > "$script"
    for i in "${idx[@]}"; do
        names="${names}${names:+_}${C_TYPE[$i]}"
        [[ "${C_TYPE[$i]}" != "idle" ]] && allidle=0
        [[ "${C_TYPE[$i]}" == "io" ]] && has_io=1
    done

    # set-up outside the measurement window: create the fio file, flush dirty pages
    if [[ $has_io -eq 1 ]]; then
        for i in "${idx[@]}"; do
            [[ "${C_TYPE[$i]}" == "io" ]] || continue
            rw="$(comp_get "${COMPS[$i]}" rw write)"
            fio --name=io_job --rw="$rw" --size=1G --create_only=1 --ioengine=libaio --blocksize="$IO_BS" > "$RAW/${CASE_ID}_w${w}_fio_create.log" 2>&1
        done
    fi
    sync

    cmd="bash '$script'"
    perf_file="$RAW/${CASE_ID}_w${w}_perf.csv"
    if [[ "$PERF_ON" == "1" && $allidle -eq 0 ]]; then
        cmd="perf stat -a -x, -o '$perf_file' -e $PERF_EVENTS -- bash '$script'"
    fi

    t_before="$(read_pkg_temp_c)"; thr_before="$(read_throttle_counts)"
    timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    out="$(bash "$MEASURE" "$cmd" "${CASE_ID}_w${w}")"
    echo "$out" > "$RAW/${CASE_ID}_w${w}_rapl.txt"
    t_after="$(read_pkg_temp_c)"; thr_after="$(read_throttle_counts)"; limits="$(read_rapl_limits)"

    # ---- after the window: counters, parsing, cleanup ----
    cpu_ops=""; cpu_req=""; vm_ops=""; vm_req=""; vm_gib=""; io_bytes=""; io_iops=""; complete=1
    for i in "${idx[@]}"; do
        case "${C_TYPE[$i]}" in
            cpu)
                IFS=, read -r o _ <<< "$(stress_metrics "${C_LOG[$i]}" cpu)"
                cpu_ops=$(awk -v a="${cpu_ops:-0}" -v b="${o:-0}" 'BEGIN { printf "%d", a + b }')
                if [[ -n "${C_REQ[$i]}" ]]; then
                    cpu_req=$(awk -v a="${cpu_req:-0}" -v b="${C_REQ[$i]}" 'BEGIN { printf "%d", a + b }')
                    [[ -n "$o" ]] && awk -v o="$o" -v r="${C_REQ[$i]}" 'BEGIN { exit !(o >= 0.99 * r) }' || complete=0
                fi
                ;;
            ram)
                IFS=, read -r o _ <<< "$(stress_metrics "${C_LOG[$i]}" vm)"
                vm_ops=$(awk -v a="${vm_ops:-0}" -v b="${o:-0}" 'BEGIN { printf "%d", a + b }')
                var="VM_OPS_PER_GIB_C${C_CHUNK[$i]}"; opg="${!var:-}"
                if [[ -n "$opg" && -n "$o" ]]; then
                    vm_gib=$(awk -v a="${vm_gib:-0}" -v o="$o" -v p="$opg" 'BEGIN { printf "%.3f", a + o / p }')
                fi
                if [[ -n "${C_REQ[$i]}" ]]; then
                    vm_req=$(awk -v a="${vm_req:-0}" -v b="${C_REQ[$i]}" 'BEGIN { printf "%d", a + b }')
                    [[ -n "$o" ]] && awk -v o="$o" -v r="${C_REQ[$i]}" 'BEGIN { exit !(o >= 0.99 * r) }' || complete=0
                fi
                ;;
            io)
                IFS=, read -r b ios _ <<< "$(fio_metrics "${C_LOG[$i]}")"
                io_bytes="$b"; io_iops="$ios"
                [[ -n "$b" ]] && awk -v o="$b" -v r="${C_REQ[$i]}" 'BEGIN { exit !(o >= 0.99 * r) }' || complete=0
                ;;
        esac
    done
    instr=""; cyc=""; rd=""; wr=""
    if [[ "$PERF_ON" == "1" && $allidle -eq 0 && -s "$perf_file" ]]; then
        IFS=, read -r instr cyc rd wr <<< "$(perf_metrics "$perf_file")"
    fi
    [[ $has_io -eq 1 ]] && rm -f io_job.*

    dur="$(kv "$out" DURATION_S)"; exit_code="$(kv "$out" EXIT_CODE)"
    [[ "$exit_code" != "0" ]] && complete=0
    IFS=, read -r thr_p_b thr_c_b <<< "$thr_before"; IFS=, read -r thr_p_a thr_c_a <<< "$thr_after"
    IFS=, read -r pl_long pl_short <<< "$limits"
    compstr="$(IFS=+; echo "${COMPS[*]}" | tr ',' ';')"
    row="${CASE_ID},${EXPERIMENT},${MODE},${LABEL},${INTENSITY},${TASK_COUNT},${CPU_LOAD},${REP},${RUN_INDEX},${w},${names},${compstr},${timestamp}"
    row="${row},$(kv "$out" START_NS),$(kv "$out" END_NS),${dur},$(kv "$out" ENERGY_UJ),$(kv "$out" ENERGY_J),$(kv "$out" DRAM_ENERGY_UJ),$(kv "$out" DRAM_ENERGY_J)"
    row="${row},$(kv "$out" CPU_UTIL_PCT),$(kv "$out" MEM_UTIL_PCT),${cpu_ops},${cpu_req},${vm_ops},${vm_req},${vm_gib},${io_bytes},${io_iops},${complete}"
    row="${row},${instr},${cyc},${rd},${wr},${t_before},${t_after},$(delta "$thr_p_b" "$thr_p_a"),$(delta "$thr_c_b" "$thr_c_a"),${pl_long},${pl_short},${exit_code}"
    echo "$row" >> "$CSV"
    echo "${CASE_ID} w${w} ${names}: ${dur}s  pkg $(kv "$out" ENERGY_J) J  dram $(kv "$out" DRAM_ENERGY_J) J  complete=${complete} exit=${exit_code}"
done
exit 0
