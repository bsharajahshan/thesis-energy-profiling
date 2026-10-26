#!/bin/bash
# lib_stubs.sh - FAKE stress-ng / fio / perf / flock / taskset / turbostat programs and a fake RAPL tree for the
# tests (sourced, not run). make_stubs <dir> creates <dir>/bin and <dir>/rapl and exports RAPL_ROOT, STUB_LOG and PATH.

make_stubs() {
T="$1"
export RAPL_ROOT="$T/rapl"
mkdir -p "$RAPL_ROOT/intel-rapl:0/intel-rapl:0:2" "$T/bin" "$T/work"
echo 1000000000 > "$RAPL_ROOT/intel-rapl:0/energy_uj"
echo 262143328850 > "$RAPL_ROOT/intel-rapl:0/max_energy_range_uj"
echo 200000000 > "$RAPL_ROOT/intel-rapl:0/constraint_0_power_limit_uw"
echo 44000000 > "$RAPL_ROOT/intel-rapl:0/constraint_1_power_limit_uw"
echo dram > "$RAPL_ROOT/intel-rapl:0/intel-rapl:0:2/name"
echo 50000000 > "$RAPL_ROOT/intel-rapl:0/intel-rapl:0:2/energy_uj"
echo 65712999613 > "$RAPL_ROOT/intel-rapl:0/intel-rapl:0:2/max_energy_range_uj"
export STUB_LOG="$T/stub_calls.log"; : > "$STUB_LOG"

cat > "$T/bin/stress-ng" <<'EOF'
#!/bin/bash
echo "stress-ng $*" >> "$STUB_LOG"
kind=""; ops=""; timeout=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cpu) kind=cpu; shift ;; --vm) kind=vm; shift ;;
        --cpu-ops|--vm-ops) ops="$2"; shift ;; --timeout) timeout="${2%s}"; shift ;;
    esac
    shift
done
[[ -n "$ops" ]] || ops=$(( ${timeout:-1} * 1000 ))
[[ -n "${STUB_SHORT:-}" ]] && ops=$(( ops / 2 ))
sleep 0.3
echo "stress-ng: info:  [100] dispatching hogs: 1 $kind"
echo "stress-ng: metrc: [100] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s"
echo "stress-ng: metrc: [100]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)"
echo "stress-ng: metrc: [100] $kind              $ops     0.30      1.00      0.10      1000.00         900.00"
echo "stress-ng: info:  [100] passed: 1: $kind (1)"
# fake energy: +2 J package, +0.1 J DRAM per call
p="$RAPL_ROOT/intel-rapl:0/energy_uj"; echo $(( $(cat "$p") + 2000000 )) > "$p"
d="$RAPL_ROOT/intel-rapl:0/intel-rapl:0:2/energy_uj"; echo $(( $(cat "$d") + 100000 )) > "$d"
EOF
cat > "$T/bin/fio" <<'EOF'
#!/bin/bash
echo "fio $*" >> "$STUB_LOG"
out=""; create=0; iosize=""
for a in "$@"; do
    case "$a" in --output=*) out="${a#--output=}" ;; --create_only=1) create=1 ;; --io_size=*) iosize="${a#--io_size=}" ;; esac
done
if [[ $create -eq 1 ]]; then echo x > io_job.0.0; exit 0; fi
case "$iosize" in *G) bytes=$(( ${iosize%G} * 1073741824 )) ;; *M) bytes=$(( ${iosize%M} * 1048576 )) ;; *) bytes=0 ;; esac
[[ -n "${STUB_SHORT:-}" ]] && bytes=$(( bytes / 2 ))
cat > "$out" <<JSON
{
  "fio version" : "fio-3.42",
  "jobs" : [
    {
      "jobname" : "io_job",
      "job options" : { "name" : "io_job", "rw" : "write", "size" : "1G" },
      "read" : {
        "io_bytes" : 0,
        "iops" : 0.000000,
        "runtime" : 0
      },
      "write" : {
        "io_bytes" : $bytes,
        "bw" : 117000,
        "iops" : 29123.500000,
        "runtime" : 64000
      },
      "usr_cpu" : 1.0,
      "sys_cpu" : 8.0
    }
  ]
}
JSON
sleep 0.3
p="$RAPL_ROOT/intel-rapl:0/energy_uj"; echo $(( $(cat "$p") + 1000000 )) > "$p"
EOF
cat > "$T/bin/perf" <<'EOF'
#!/bin/bash
echo "perf $*" >> "$STUB_LOG"
case " $* " in *" -- "*) ;; *) echo "perf stub"; exit 0 ;; esac
f=""; while [[ "$1" != "--" ]]; do [[ "$1" == "-o" ]] && f="$2"; shift; done; shift
"$@"; rc=$?
printf '%s\n' "1000000000,,instructions,300000000,100.00,," "800000000,,cycles,300000000,100.00,," \
  "124.50,MiB,uncore_imc/data_reads/,300000000,100.00,," "68.25,MiB,uncore_imc/data_writes/,300000000,100.00,," "<not counted>,,foo,0,0,," > "$f"
exit $rc
EOF
printf '#!/bin/bash\nexit 0\n' > "$T/bin/flock"
printf '#!/bin/bash\necho "taskset $*" >> "$STUB_LOG"\nshift 2\nexec "$@"\n' > "$T/bin/taskset"
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH"

cat > "$T/bin/turbostat" <<'EOF2'
#!/bin/bash
echo "turbostat $*" >> "$STUB_LOG"
[[ "$1" == "--version" || "$*" == *"--list"* ]] && { echo "turbostat version stub"; exit 0; }
n=-1; while [[ $# -gt 0 ]]; do [[ "$1" == "--num_iterations" ]] && n="$2"; shift; done
echo "Time_Of_Day_Seconds Avg_MHz Bzy_MHz Busy% PkgTmp PkgWatt RAMWatt"
i=0
while [[ $n -lt 0 || $i -lt $n ]]; do echo "1793000000.$i 100 2900 10.0 60 5.00 0.30"; i=$((i + 1)); sleep 1; done
EOF2
chmod +x "$T/bin/"*
}
