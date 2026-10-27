#!/bin/bash
# pilot_a.sh - pilot, part A (2026-10-26). Run ONCE on the test laptop before the main session.
#
# What it does (about 50 minutes; --smoke: about 3 minutes, to check that the script itself works):
#   1. snapshot of the machine (CPU topology, power settings, RAPL limits, thermald, running processes)
#   2. capability checks (each one PASS/FAIL in checks.txt): RAPL read, perf with uncore_imc, turbostat
#      columns, flock, taskset, fio create_only / io_size, throttle counters, package temperature
#   3. how stress-ng counts operations with several workers (--cpu-ops)
#   4. scan of CPU methods and memory (vm) methods: ops/s, power, DRAM traffic -> scan.csv
#   5. fio fixed-work runs (duration of an 8 GiB write)
#   6. hot probe: 8 CPU threads while sampling temperature, RAPL limits and throttle counters,
#      then 10 minutes of cool-down sampling
# Nothing here is a result of the thesis; the output only decides the settings in calibration.env.
#
# Usage (from the repo root; needs sudo for perf, turbostat and RAPL access):
#   sudo -v
#   bash scripts/pilot_a.sh [--smoke] [output_dir]
# Send the resulting <output_dir>.tar.gz back.
#
# Tests without the real tools: scripts/tests/run_pilot_smoke.sh

set -u
export LC_ALL=C
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shlib/parsers.sh"
source "$SCRIPT_DIR/shlib/sensors.sh"

SMOKE=0
if [[ "${1:-}" == "--smoke" ]]; then SMOKE=1; shift; fi
OUT="${1:-pilot_a_$(date -u +%Y%m%dT%H%MZ)}"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
SECS=20; REPS=2; PAUSE=20; HOT_S=90; COOL_S=600; FIO_BIG_GIB=8
if [[ $SMOKE -eq 1 ]]; then SECS=4; REPS=1; PAUSE=2; HOT_S=10; COOL_S=20; FIO_BIG_GIB=1; fi
SUDO="${PILOT_SUDO-sudo -n}"
RAPL_ROOT="${RAPL_ROOT:-/sys/class/powercap/intel-rapl}"
PERF_EVENTS="instructions,cycles,uncore_imc/data_reads/,uncore_imc/data_writes/"
SCRATCH="$(mktemp -d "$OUT/scratch.XXXXXX")"
trap 'rm -rf "$SCRATCH"; [[ -n "${TS_PID:-}" ]] && kill "$TS_PID" 2>/dev/null; [[ -n "${SAMPLER_PID:-}" ]] && kill "$SAMPLER_PID" 2>/dev/null' EXIT
cd "$SCRATCH" || exit 1   # fio files go here

say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/pilot_a.log"; }
verdict() { echo "$1 $2${3:+  ($3)}" >> "$OUT/checks.txt"; say "$1 $2"; }

say "pilot_a start, smoke=$SMOKE, output $OUT"

# ---- 1. snapshot -----------------------------------------------------------------------------------------
say "1/6 snapshot"
{
    echo "== date"; date -u; echo "== kernel"; uname -a
    echo "== lscpu"; lscpu 2>&1
    echo "== lscpu -e"; lscpu -e=CPU,CORE,SOCKET,ONLINE,MAXMHZ,MINMHZ 2>&1
    echo "== thread siblings"; cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list 2>&1 | sort -u
    echo "== cpufreq (cpu0)"
    for f in scaling_driver scaling_governor energy_performance_preference scaling_min_freq scaling_max_freq cpuinfo_max_freq; do
        echo "$f=$(cat /sys/devices/system/cpu/cpu0/cpufreq/$f 2>&1)"
    done
    echo "== intel_pstate"
    for f in /sys/devices/system/cpu/intel_pstate/*; do [[ -f "$f" ]] && echo "$(basename "$f")=$(cat "$f" 2>&1)"; done
    echo "== power profile"; powerprofilesctl get 2>&1
    echo "== AC"; for f in /sys/class/power_supply/*/online; do echo "$f=$(cat "$f" 2>&1)"; done
    echo "== RAPL tree"
    for d in "$RAPL_ROOT"/intel-rapl:0 "$RAPL_ROOT"/intel-rapl:0/intel-rapl:0:*; do
        [[ -d "$d" ]] || continue
        echo "-- $d"
        for f in "$d"/*; do
            [[ -f "$f" && -r "$f" ]] || continue
            case "$(basename "$f")" in uevent|energy_uj) continue ;; esac
            echo "$(basename "$f")=$(cat "$f" 2>&1)"
        done
    done
    echo "== thermald"; systemctl is-active thermald 2>&1; ls -l /etc/thermald 2>&1
    cat /etc/thermald/thermal-conf.xml 2>&1 | head -150
    echo "== thermald journal (last 60 lines)"; journalctl -u thermald --no-pager -n 60 2>&1
    echo "== thermal zones"; for z in /sys/class/thermal/thermal_zone*; do echo "$z type=$(cat "$z/type" 2>&1) temp=$(cat "$z/temp" 2>&1)"; done
    echo "== throttle counters"; ls /sys/devices/system/cpu/cpu0/thermal_throttle/ 2>&1; read_throttle_counts
    echo "== perf"; perf --version 2>&1; echo "paranoid=$(cat /proc/sys/kernel/perf_event_paranoid 2>&1)"; perf list 2>/dev/null | grep -i imc
    echo "== versions"; stress-ng --version 2>&1; fio --version 2>&1; turbostat --version 2>&1; nextflow -version 2>&1 | head -5; python3 --version 2>&1; flock --version 2>&1 | head -1
    echo "== stress-ng methods"; stress-ng --cpu-method help 2>&1; stress-ng --vm-method help 2>&1
    echo "== turbostat columns"; $SUDO turbostat --list 2>&1 | head -80
    echo "== memory"; free -m 2>&1; echo "== disk"; df -h . 2>&1
    echo "== top processes"; ps -eo pid,pcpu,pmem,comm --sort=-pcpu 2>&1 | head -20
} > "$OUT/snapshot.txt" 2>&1

# ---- 2. capability checks ---------------------------------------------------------------------------------
say "2/6 capability checks"
: > "$OUT/checks.txt"
# The energy counters are readable only by root by default (as in the earlier sessions: chmod a+r).
# /sys/class/powercap/intel-rapl is a symlink, and find does not follow a symlink that is given as the start path
# (this made the first smoke test on 2026-10-27 fail), so find starts at the real folder. The second command is a
# direct chmod of the files that are read, as a backup.
RAPL_REAL="$(readlink -f "$RAPL_ROOT" 2>/dev/null || echo "$RAPL_ROOT")"
$SUDO find "$RAPL_REAL" -name energy_uj -exec chmod a+r {} + >/dev/null 2>&1
$SUDO chmod a+r "$RAPL_ROOT/intel-rapl:0/energy_uj" "$RAPL_ROOT"/intel-rapl:0/intel-rapl:0:*/energy_uj >/dev/null 2>&1
{ echo "RAPL_ROOT=$RAPL_ROOT"; echo "RAPL_REAL=$RAPL_REAL"; ls -l "$RAPL_ROOT/intel-rapl:0/energy_uj" "$RAPL_ROOT"/intel-rapl:0/intel-rapl:0:*/energy_uj 2>&1; } > "$OUT/check_rapl_permissions.txt"
[[ -r "$RAPL_ROOT/intel-rapl:0/energy_uj" ]] && verdict PASS rapl_package_readable || verdict FAIL rapl_package_readable "see check_rapl_permissions.txt"
dram_ok=0
for d in "$RAPL_ROOT"/intel-rapl:0/intel-rapl:0:*; do
    [[ "$(cat "$d/name" 2>/dev/null)" == "dram" && -r "$d/energy_uj" ]] && dram_ok=1
done
[[ $dram_ok -eq 1 ]] && verdict PASS rapl_dram_readable || verdict FAIL rapl_dram_readable
$SUDO sysctl -w kernel.perf_event_paranoid=-1 >/dev/null 2>&1 && verdict PASS perf_paranoid_set || verdict FAIL perf_paranoid_set "run: sudo sysctl -w kernel.perf_event_paranoid=-1"
perf stat -a -x, -o "$OUT/check_perf.csv" -e "$PERF_EVENTS" -- sleep 1 >/dev/null 2>&1
IFS=, read -r pi pc pr pw <<< "$(perf_metrics "$OUT/check_perf.csv" 2>/dev/null)"
if [[ -n "${pi:-}" && -n "${pc:-}" && -n "${pr:-}" && -n "${pw:-}" ]]; then verdict PASS perf_four_events "instructions, cycles, uncore_imc reads and writes counted without root"; else verdict FAIL perf_four_events "see check_perf.csv"; fi
COLS="Time_Of_Day_Seconds,Avg_MHz,Bzy_MHz,Busy%,PkgTmp,PkgWatt,RAMWatt"
$SUDO turbostat --quiet --Summary --interval 1 --num_iterations 3 --show "$COLS" > "$OUT/check_turbostat.txt" 2>&1
if [[ "$(grep -c '^[0-9]' "$OUT/check_turbostat.txt")" -ge 3 ]]; then verdict PASS turbostat_columns "$COLS"; else
    verdict FAIL turbostat_columns "$COLS"
    $SUDO turbostat --quiet --Summary --interval 1 --num_iterations 3 --show "Avg_MHz,Bzy_MHz,Busy%,PkgTmp,PkgWatt,RAMWatt" > "$OUT/check_turbostat_no_tod.txt" 2>&1
    [[ "$(grep -c '^[0-9]' "$OUT/check_turbostat_no_tod.txt")" -ge 3 ]] && verdict PASS turbostat_columns_without_time_of_day || verdict FAIL turbostat_columns_without_time_of_day
fi
command -v flock >/dev/null && verdict PASS flock_present || verdict FAIL flock_present
command -v taskset >/dev/null && taskset -c 0 true && verdict PASS taskset_works || verdict FAIL taskset_works
command -v nextflow >/dev/null && verdict PASS nextflow_present || verdict FAIL nextflow_present
command -v python3 >/dev/null && verdict PASS python3_present || verdict FAIL python3_present
[[ "$(read_pkg_temp_c)" != "NA" ]] && verdict PASS package_temperature_sensor || verdict FAIL package_temperature_sensor
[[ "$(read_throttle_counts)" != "NA,NA" ]] && verdict PASS throttle_counters "$(read_throttle_counts)" || verdict FAIL throttle_counters
[[ "$(read_rapl_limits)" != "NA,NA" ]] && verdict PASS rapl_limits_readable "$(read_rapl_limits)" || verdict FAIL rapl_limits_readable
fio --name=io_job --rw=write --size=256M --create_only=1 --ioengine=libaio --blocksize=4k > "$OUT/check_fio_create.log" 2>&1
[[ -s io_job.0.0 ]] && verdict PASS fio_create_only || verdict FAIL fio_create_only
fio --name=io_job --rw=write --size=256M --io_size=512M --ioengine=libaio --blocksize=4k --iodepth=16 --end_fsync=1 --output-format=json --output="$OUT/check_fio.json" > "$OUT/check_fio_run.log" 2>&1
IFS=, read -r fb _ <<< "$(fio_metrics "$OUT/check_fio.json")"
[[ "${fb:-0}" -ge 536870912 ]] && verdict PASS fio_io_size_end_fsync "wrote $fb bytes" || verdict FAIL fio_io_size_end_fsync "wrote ${fb:-none}"
rm -f io_job.*

# ---- helpers for the scans ------------------------------------------------------------------------------------
echo "name,kind,method,threads,chunk_gb,rep,duration_s,energy_j,dram_energy_j,pkg_w,ops,real_s,ops_per_s,instructions,cycles,dram_read_mib,dram_write_mib,temp_before_c,temp_after_c,exit_code,start_ns,end_ns" > "$OUT/scan.csv"
mkdir -p "$OUT/raw"

# scan <name> <kind: cpu|vm|io> <method> <threads> <chunk_gb> <rep> <command...>
scan() {
    local name="$1" kind="$2" method="$3" threads="$4" chunk="$5" rep="$6"; shift 6
    local cmd="$*" d="$OUT/raw/${name}_r${rep}" out tb ta ops real ops_s pi pc pr pw
    mkdir -p "$d"
    echo "#!/bin/bash" > "$d/cmd.sh"; echo "$cmd" >> "$d/cmd.sh"
    sync
    tb="$(read_pkg_temp_c)"
    out="$(WORKLOAD_LOG="$d/workload.log" bash "$SCRIPT_DIR/measure_rapl.sh" \
        "perf stat -a -x, -o '$d/perf.csv' -e $PERF_EVENTS -- bash '$d/cmd.sh'" "$name" 2>"$d/measure.err")"
    ta="$(read_pkg_temp_c)"
    echo "$out" > "$d/rapl.txt"
    ops=""; real=""; ops_s=""
    case "$kind" in
        cpu|vm) IFS=, read -r ops real _ <<< "$(stress_metrics "$d/workload.log" "$kind")" ;;
        io)     IFS=, read -r ops ops_s real <<< "$(fio_metrics "$d/workload.log")" ;;
    esac
    [[ "$kind" != "io" && -n "$ops" && -n "$real" ]] && ops_s="$(awk -v o="$ops" -v r="$real" 'BEGIN { if (r > 0) printf "%.2f", o / r }')"
    IFS=, read -r pi pc pr pw <<< "$(perf_metrics "$d/perf.csv")"
    echo "${name},${kind},${method},${threads},${chunk},${rep},$(kv "$out" DURATION_S),$(kv "$out" ENERGY_J),$(kv "$out" DRAM_ENERGY_J),$(kv "$out" POWER_W),${ops},${real},${ops_s},${pi},${pc},${pr},${pw},${tb},${ta},$(kv "$out" EXIT_CODE),$(kv "$out" START_NS),$(kv "$out" END_NS)" >> "$OUT/scan.csv"
    say "  $name rep $rep: $(kv "$out" DURATION_S) s, $(kv "$out" POWER_W) W, ops=${ops:-?}, ops/s=${ops_s:-?}"
    rm -f io_job.*
    sleep "$PAUSE"
}

# session-wide turbostat log for the whole scan (same columns as the main session will use)
$SUDO turbostat --quiet --Summary --interval 2 --show "$COLS" > "$OUT/turbostat_pilot.log" 2>"$OUT/turbostat_pilot.err" &
TS_PID=$!
date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/turbostat_pilot_start.txt"; date +%s.%N >> "$OUT/turbostat_pilot_start.txt"
sleep 3

# ---- 3. how does --cpu-ops count with several workers? -----------------------------------------------
say "3/6 cpu-ops semantics (does --cpu-ops count all workers together?)"
OPS_TEST=$([[ $SMOKE -eq 1 ]] && echo 2000 || echo 20000)
for T in 8 1; do
    scan "ops_semantics_cpu${T}" cpu matrixprod "$T" "" 1 "stress-ng --cpu $T --cpu-method matrixprod --cpu-ops $OPS_TEST --metrics-brief --timeout 300s"
done
scan "ops_semantics_vm" vm write64 1 8 1 "stress-ng --vm 1 --vm-bytes 8G --vm-method write64 --vm-ops 2000 --metrics-brief --timeout 300s"

# ---- 4. method scans -------------------------------------------------------------------------------------
say "4/6 CPU method scan"
CPU_METHODS="matrixprod int64 double fft crc16 ackermann all"
[[ $SMOKE -eq 1 ]] && CPU_METHODS="matrixprod all"
for m in $CPU_METHODS; do
    for T in 8 1; do
        for r in $(seq 1 $REPS); do
            scan "cpu_${m}_t${T}" cpu "$m" "$T" "" "$r" "stress-ng --cpu $T --cpu-method $m --metrics-brief --timeout ${SECS}s"
        done
    done
done
say "4/6 memory (vm) method scan"
VM_METHODS="write64 read64 flip walk-1d swap inc-nybble all"
[[ $SMOKE -eq 1 ]] && VM_METHODS="write64 all"
for m in $VM_METHODS; do
    for C in 8 1; do
        for r in $(seq 1 $REPS); do
            scan "vm_${m}_c${C}" vm "$m" 1 "$C" "$r" "stress-ng --vm 1 --vm-bytes ${C}G --vm-method $m --metrics-brief --timeout ${SECS}s"
        done
    done
done

# ---- 5. fio fixed-work runs -----------------------------------------------------------------------------
say "5/6 fio fixed-work runs (${FIO_BIG_GIB} GiB)"
for rw in write randwrite; do
    fio --name=io_job --rw=$rw --size=1G --create_only=1 --ioengine=libaio --blocksize=4k > /dev/null 2>&1; sync
    scan "fio_${rw}_${FIO_BIG_GIB}g" io "$rw" "" "" 1 "fio --ioengine=libaio --iodepth=16 --blocksize=4k --name=io_job --rw=$rw --size=1G --io_size=${FIO_BIG_GIB}G --end_fsync=1 --output-format=json --output=$SCRATCH/fio_out.json; cat $SCRATCH/fio_out.json"
done

# ---- 6. hot probe and cool-down -------------------------------------------------------------------------
say "6/6 hot probe (${HOT_S} s of 8 CPU threads) and cool-down (${COOL_S} s)"
echo "epoch,phase,temp_c,pl_long_w,pl_short_w,throttle_pkg,throttle_core,cur_freq_khz" > "$OUT/hot_probe.csv"
phase="hot"
(
    while true; do
        IFS=, read -r a b <<< "$(read_rapl_limits)"; IFS=, read -r c d <<< "$(read_throttle_counts)"
        echo "$(date +%s),$(cat "$SCRATCH/phase" 2>/dev/null),$(read_pkg_temp_c),$a,$b,$c,$d,$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)"
        sleep 2
    done
) >> "$OUT/hot_probe.csv" 2>/dev/null &
SAMPLER_PID=$!
echo hot > "$SCRATCH/phase"
scan "hot_probe_cpu8" cpu matrixprod 8 "" 1 "stress-ng --cpu 8 --cpu-method matrixprod --metrics-brief --timeout ${HOT_S}s"
echo cool > "$SCRATCH/phase"
sleep "$COOL_S"
kill "$SAMPLER_PID" 2>/dev/null; SAMPLER_PID=""

kill "$TS_PID" 2>/dev/null; TS_PID=""
say "done; packing"
cd "$OUT/.." || exit 1
tar czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")" && say "wrote $OUT.tar.gz - please send this file back"
echo "checks:"; cat "$OUT/checks.txt"
