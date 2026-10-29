#!/usr/bin/env bash

# test_session.sh - whole sessions with run.sh, session.sh, preflight.py and the REAL Nextflow
#
# On the fake laptop of fake_machine.sh (stand-in tools, fake sysfs in the
# real layout), with a tiny schedule (8 runs) and tiny job sizes. run.sh is
# started like the agent will start it: no terminal (setsid), almost empty
# environment (env -i, PATH=/usr/bin:/bin), stdin /dev/null.
#   S1 normal session: an "agent" still runs at the start, the session waits
#      for it; second call while running; end: DONE, archive, clean-up
#   S2 clean
#   S3 start check with two problems: both reported at once, nothing started
#   S4 crash in the middle (everything killed), the same command continues;
#      clean is refused while running
#   S5 power cable out and battery low: stops cleanly; plugged in: continues
#   S6 runs that always fail: retried, repair pass, MISSING in the summary
#   S7 problems planted in a finished run (wrong governor, overlapping window):
#      the end check finds them, the summary says CHECK FAILED
#   S8 pilot: probes, calibration, repeats, rehearsal; the injected failed
#      attempt and the killed Nextflow call are handled (pilot_report.txt)
# WSL1 has no systemd, so the setsid fallback of run.sh is what runs here.
#
# Usage: NEXTFLOW=/path/to/nextflow NXF_HOME=/path bash scripts/bench/tests/test_session.sh

set -u
export LC_ALL=C
NEXTFLOW="${NEXTFLOW:-$(command -v nextflow)}"
BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=fake_machine.sh
source "$BENCH/tests/fake_machine.sh"
ROOT="$(mktemp -d /tmp/session_test.XXXXXX)"
PASS=0
FAIL=0

kill_everything() {   # like a power cut: every process of this test except the test itself
    local p
    for p in $(pgrep -f -- "$BENCH/session.sh|$ROOT/" 2>/dev/null); do
        [[ "$p" != "$$" ]] && kill -9 "$p" 2>/dev/null
    done
    sleep 2
}
finish_all() {
    kill_everything
    fake_machine_stop
    rm -rf "$ROOT"
}
trap finish_all EXIT

ok()  { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

fake_machine_start "$ROOT/fm"
RES="$ROOT/results"
SES="$RES/main"

cat > "$ROOT/cal.env" <<'EOF'
CPU_OPS=8000
VM_OPS_1G=80
VM_OPS_2G=80
VM_OPS_4G=80
VM_OPS_8G=80
IO_BYTES=1048576
MECH_FIXED_S=1
IDLE_S=1
CPU_S_T1=1
CPU_S_T2=1
CPU_S_T4=1
CPU_S_T8=1
CPU_S_T8_LOAD50=1
CPU_S_T8_LOAD25=1
VM_S=1
IO_S_SEQ=1
IO_S_RAND=1
POWER_PROFILE=balanced
GOVERNOR_MAIN=powersave
EPP_MAIN=balance_performance
GOVERNOR_CONTROL=performance
OPS_SEMANTICS=total
SETTLE_TEMP_C=55
SETTLE_MIN_S=0
SETTLE_MAX_S=3
SETTLE_FALLBACK_S=1
OTHER_CPU_LIMIT=0.10
DISK_KEEP_FREE_KB=1024
WATCHDOG_FACTOR=3
WATCHDOG_MIN_EXTRA_S=5
MAX_ATTEMPTS=3
AC_WAIT_MAX_S=2
BATTERY_STOP_PCT=30
POWER_POLL_S=1
GRACE_MAX_S=30
WAIT_FOR_GONE='fakeagent'
BG_CPU_MAX=0.5
SETTLE_EST_S=0
NXF_START_S=5
BREAK_S=2
EOF
cat > "$ROOT/sched.csv" <<'EOF'
# test schedule: 8 runs, two rounds
order,run_id,phase,round,position,case_id,part_order
1,idle_start_1,idle_start,,1,idle,
2,r1_cpu_t8,round,1,1,cpu_t8,
3,r1_rq2_cpu_ram_seq,round,1,2,rq2_cpu_ram_seq,"1,0"
4,r1_mem_1g,round,1,3,mem_1g,
5,r1_io_seq,round,1,4,io_seq,
6,r2_mem_1g,round,2,1,mem_1g,
7,r2_cpu_t8,round,2,2,cpu_t8,
8,idle_end_1,idle_end,,1,idle,
EOF
ORDER="idle_start_1 r1_cpu_t8 r1_rq2_cpu_ram_seq r1_mem_1g r1_io_seq r2_mem_1g r2_cpu_t8 idle_end_1"

# runsh ARGS - run.sh as the agent would call it; sets OUT, RC
runsh() {
    OUT="$(setsid -w env -i HOME="$HOME" PATH=/usr/bin:/bin BENCH_RESULTS="$RES" BENCH_SCHEDULE="$ROOT/sched.csv" \
        BENCH_CALIBRATION="$ROOT/cal.env" BENCH_SYSROOT="$R" BENCH_EXTRA_PATH="$BIN:$(dirname "$NEXTFLOW")" \
        BENCH_IO_DIR="$T/io" BENCH_DISK_STAT="$R/sys/block/sdb/stat" BENCH_SKIP_GIT=1 \
        NXF_HOME="${NXF_HOME:-$HOME/.nextflow}" NXF_VER=26.04.4 \
        bash "$BENCH/run.sh" "$@" < /dev/null 2>&1)"
    RC=$?
}

state_is() { grep -qs "^status=$1\$" "$SES/state.txt"; }
wait_state() {   # STATUS MAX_SECONDS
    local i
    for ((i = 0; i < $2; i += 2)); do
        state_is "$1" && return 0
        sleep 2
    done
    return 1
}
wait_progress() {   # N_LINES MAX_SECONDS
    local i
    for ((i = 0; i < $2; i += 1)); do
        [[ "$({ wc -l < "$SES/progress.txt"; } 2>/dev/null || echo 0)" -ge "$1" ]] && return 0
        sleep 1
    done
    return 1
}
out_has() { grep -q -e "$1" <<< "$OUT"; }
first_line_is() { [[ "$(head -n 1 "$SES/summary.txt" 2>/dev/null)" == "$1"* ]]; }
in_archive() { tar -tzf "$RES/thesis_main.tar.gz" 2>/dev/null | grep -qx "$1"; }
env_is() {   # KEY VALUE - as session.env gives it to the scripts (quoted or not)
    python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import benchlib
sys.exit(0 if benchlib.read_env(sys.argv[2]).get(sys.argv[3]) == sys.argv[4] else 1)' "$BENCH" "$SES/session.env" "$1" "$2"
}
no_leftovers() {   # the session script may need a moment to exit after writing "done"
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f -- "$ROOT/" > /dev/null || return 0
        sleep 1
    done
    pgrep -af -- "$ROOT/" | sed 's/^/    left: /'
    return 1
}
show() {
    echo "    --- run.sh output:"; sed 's/^/    /' <<< "$OUT" | tail -n 25
    echo "    --- session.log:"; tail -n 15 "$SES/session.log" 2>/dev/null | sed 's/^/    /'
    head -n 8 "$SES/summary.txt" 2>/dev/null | sed 's/^/    summary: /'
}

echo "S1. normal session; an 'agent' is still running at the start"
f0=$FAIL
cp "$(command -v sleep)" "$BIN/fakeagent"
"$BIN/fakeagent" 8 &
t_start=$SECONDS
runsh main
took=$((SECONDS - t_start))
check "run.sh returns soon (${took} s, start check included) with STARTED" \
    bash -c "(( $RC == 0 && $took < 90 )) && grep -q '^STARTED' <<< \"\$1\"" _ "$OUT"
check "detached with setsid (no systemd in WSL1)" out_has "running in the background (setsid)"
check "start check passed" out_has "RESULT: READY"
check "the start check found the RAPL folder behind its symlink" env_is RAPL_PKG_DIR "$F_PKG"
check "the start check found perf with interval summaries" env_is PERF_SUMMARY 1
runsh main
check "second call while running: RUNNING, nothing new" out_has "^RUNNING"
check "session ends DONE" wait_state done 400
check "summary: DONE" first_line_is DONE
check "it waited for the agent to close first" \
    bash -c "tar -xOzf '$RES/thesis_main.tar.gz' main/waits.txt | grep -q 'still running: \[fakeagent\]'"
check "no start warning (the agent closed in time)" bash -c "! grep -q 'at the start:' '$SES/summary.txt'"
check "archive and checksum written" bash -c "[[ -s '$RES/thesis_main.tar.gz' && -s '$RES/thesis_main.tar.gz.sha256' ]] && cd '$RES' && sha256sum -c --quiet thesis_main.tar.gz.sha256"
check "archive holds the results" in_archive "main/runs/r1_cpu_t8/a1/result.csv"
check "archive holds the real perf test output" in_archive "main/preflight/perf_interval_summary.csv"
check "archive holds the cooling breaks" in_archive "main/breaks.txt"
check "archive holds only the session folder" bash -c "! tar -tzf '$RES/thesis_main.tar.gz' | grep -qv '^main/'"
check "no Nextflow work folder in the archive" bash -c "! tar -tzf '$RES/thesis_main.tar.gz' | grep -q 'nextflow/work'"
check "runs in schedule order" bash -c "[[ \"\$(tar -xOzf '$RES/thesis_main.tar.gz' main/progress.txt | awk '{ printf \"%s \", \$2 }')\" == '$ORDER ' ]]"
check "all 8 runs VALID in one attempt" bash -c "[[ \$(tar -xOzf '$RES/thesis_main.tar.gz' main/progress.txt | grep -c 'VALID round=.* attempts=1 ') == 8 ]]"
check "end check passed" bash -c "tar -xOzf '$RES/thesis_main.tar.gz' main/check.txt | grep -qx 'RESULT: PASSED'"
check "one combined results table: header + 8 runs" bash -c "[[ \$(tar -xOzf '$RES/thesis_main.tar.gz' main/results_all.csv | wc -l) == 9 ]]"
check "combined job table in the archive" in_archive main/parts_all.csv
check "attempt table in the archive" in_archive main/attempts_all.csv
check "summary shows the end check" grep -q "^  RESULT: PASSED" "$SES/summary.txt"
check "two cooling breaks (round 1 -> 2, before the idle end)" bash -c "[[ \$(tar -xOzf '$RES/thesis_main.tar.gz' main/session.log | grep -c 'cooling break of') == 2 ]]"
check "working folder deleted, only summary and state left" bash -c "[[ \"\$(ls '$SES' | tr '\n' ' ')\" == 'state.txt summary.txt ' ]]"
check "fio folder deleted" test ! -e "$T/io"
check "no processes left" no_leftovers
runsh status
check "status: DONE and the file to send" bash -c "grep -q '^main: DONE' <<< \"\$1\" && grep -q 'send this file: $RES/thesis_main.tar.gz' <<< \"\$1\"" _ "$OUT"
runsh main
check "start after the end: DONE, nothing new" out_has "^DONE: this session has finished"
(( FAIL > f0 )) && show

echo "S2. clean"
f0=$FAIL
runsh clean
check "CLEANED" out_has "^CLEANED"
check "results folder gone" test ! -e "$RES"
(( FAIL > f0 )) && show

echo "S3. start check with two problems at once"
f0=$FAIL
put "$F_AC" 0
put "$F_DRAM/name" notdram
runsh main
put "$F_AC" 1
put "$F_DRAM/name" dram
check "exit 1, NOT STARTED" bash -c "(( $RC == 1 )) && grep -q '^NOT STARTED' <<< \"\$1\"" _ "$OUT"
check "power cable problem reported" out_has "^FAIL  power .*plug it in"
check "DRAM problem reported in the same report" out_has "^FAIL  rapl .*no DRAM domain"
check "no session started" bash -c "[[ ! -e '$SES/state.txt' ]]"
(( FAIL > f0 )) && show
runsh clean > /dev/null

echo "S4. crash in the middle, the same command continues"
f0=$FAIL
runsh main
check "STARTED" out_has "^STARTED"
check "three runs done before the crash" wait_progress 3 200
runsh clean
check "clean refused while running" bash -c "(( $RC == 1 )) && grep -q '^NOT CLEANED' <<< \"\$1\"" _ "$OUT"
before="$(awk '$3 == "VALID" { printf "%s ", $2 }' "$SES/progress.txt")"
kill_everything
check "nothing runs after the crash" no_leftovers
runsh main
check "continued: STARTED again" bash -c "grep -q '^Continuing' <<< \"\$1\" && grep -q '^STARTED' <<< \"\$1\"" _ "$OUT"
check "session ends DONE" wait_state done 400
check "summary: DONE" first_line_is DONE
check "runs done before the crash were not repeated" bash -c "for r in $before; do [[ \$(tar -tzf '$RES/thesis_main.tar.gz' | grep -c \"^main/runs/\$r/a[0-9]*/\$\") == 1 ]] || exit 1; done"
check "no processes left" no_leftovers
(( FAIL > f0 )) && show
runsh clean > /dev/null

echo "S5. power cable out with a low battery: stops; plugged in: continues"
f0=$FAIL
runsh main
check "STARTED" out_has "^STARTED"
check "two runs done" wait_progress 2 200
put "$F_AC" 0
put "$F_BAT" 10
check "session stops cleanly" wait_state stopped 200
check "summary says STOPPED and why" first_line_is "STOPPED: no power cable and the battery is at 10 %"
runsh status
check "status shows STOPPED" out_has "^main: STOPPED"
put "$F_AC" 1
put "$F_BAT" 80
runsh main
check "continued: STARTED" out_has "^STARTED"
check "session ends DONE" wait_state done 400
check "summary: DONE" first_line_is DONE
(( FAIL > f0 )) && show
runsh clean > /dev/null

echo "S6. memory runs always fail: retried, repair pass, MISSING"
f0=$FAIL
echo vm > "$CTL/short_stressors"
runsh main
check "STARTED" out_has "^STARTED"
check "session ends (done, with missing runs)" wait_state done 600
rm -f "$CTL/short_stressors"
check "summary: MISSING with the three runs that use the memory job" first_line_is "MISSING: 3 of 8 runs: r1_rq2_cpu_ram_seq r1_mem_1g r2_mem_1g"
check "reason given (work not complete)" grep -q "r1_mem_1g: work_incomplete" "$SES/summary.txt"
check "3 attempts + 3 in the repair pass" bash -c "[[ \$(tar -tzf '$RES/thesis_main.tar.gz' | grep -c '^main/runs/r1_mem_1g/a[0-9]*/\$') == 6 ]]"
check "the other runs are fine" bash -c "tar -tzf '$RES/thesis_main.tar.gz' | grep -q '^main/runs/r2_cpu_t8/VALID\$'"
check "archive still written" test -s "$RES/thesis_main.tar.gz"
(( FAIL > f0 )) && show

echo "S7. planted problems: a finished run gets a wrong governor and a window that overlaps the next runs"
f0=$FAIL
runsh clean > /dev/null
runsh main
check "STARTED" out_has "^STARTED"
planted=0
for _ in $(seq 1 400); do
    att="$(cat "$SES/runs/r1_cpu_t8/VALID" 2>/dev/null)"
    if [[ -n "$att" && -s "$SES/runs/r1_cpu_t8/$att/result.csv" ]]; then
        python3 -I - "$SES/runs/r1_cpu_t8/$att/result.csv" <<'PY'
import csv, sys
path = sys.argv[1]
with open(path, newline="") as f:
    rows = list(csv.DictReader(f))
rows[0]["governor_start"] = "performance"
rows[0]["t_end_us"] = str(int(rows[0]["t_end_us"]) + 600_000_000)     # 10 min longer: overlaps the next runs
with open(path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
PY
        planted=1
        break
    fi
    sleep 0.5
done
check "problems planted while the session ran" test "$planted" -eq 1
check "session ends" wait_state done 400
check "summary: CHECK FAILED for the overlap and the governor" first_line_is "CHECK FAILED: overlap, conditions (see check.txt)"
check "check names the run with the wrong governor" bash -c "tar -xOzf '$RES/thesis_main.tar.gz' main/check.txt | grep -q 'r1_cpu_t8: governor performance instead of powersave'"
check "check names the overlapping windows" bash -c "tar -xOzf '$RES/thesis_main.tar.gz' main/check.txt | grep -q '^FAIL  overlap .*r1_cpu_t8/a1 and r1_rq2_cpu_ram_seq/a1'"
check "archive still written" test -s "$RES/thesis_main.tar.gz"
(( FAIL > f0 )) && show

echo "S8. pilot: probes, calibration, repeats, rehearsal with the two recovery tests"
f0=$FAIL
runsh clean > /dev/null
cat > "$ROOT/pilot.csv" <<'EOF'
# test pilot: 7 probes, 2 repeats, a 3-run rehearsal round
order,run_id,phase,round,position,case_id,part_order
1,probe_idle,probe,,1,probe_idle,
2,probe_ops_semantics,probe,,2,probe_ops_semantics,
3,probe_cpu_t8,probe,,3,probe_cpu_t8,
4,probe_mem_8g,probe,,4,probe_mem_8g,
5,probe_mem_1g,probe,,5,probe_mem_1g,
6,probe_io_seq,probe,,6,probe_io_seq,
7,probe_cooldown,probe,,7,probe_cooldown,
8,rep1_cpu_t8,repeat,,8,cpu_t8,
9,rep1_mem_8g,repeat,,9,mem_8g,
10,idle_start_1,idle_start,,1,idle,
11,r1_cpu_t2,round,1,1,cpu_t2,
12,r1_mem_1g,round,1,2,mem_1g,
13,r1_cpu_t8,round,1,3,cpu_t8,
14,idle_end_1,idle_end,,1,idle,
EOF
# Fixed-work stand-in runs take 15 s here, so the kill (3 s after the window opens)
# lands in the middle of a run as it would with real 60 s jobs; the watchdog gets
# room for that (WATCHDOG_MIN_EXTRA_S, the later value of a key wins).
{ cat "$ROOT/cal.env"; printf 'PROBE_S=1\nPROBE_IO_BYTES=1048576\nPROBE_COOLDOWN_S=3\nPROBE_SEMANTICS_OPS=2000\nWATCHDOG_MIN_EXTRA_S=60\n'; } \
    > "$ROOT/cal_pilot.env"
echo 15 > "$CTL/stress_secs"
SES="$RES/pilot"
PA="$RES/thesis_pilot.tar.gz"
OUT="$(setsid -w env -i HOME="$HOME" PATH=/usr/bin:/bin BENCH_RESULTS="$RES" BENCH_SCHEDULE="$ROOT/pilot.csv" \
    BENCH_CALIBRATION="$ROOT/cal_pilot.env" BENCH_SYSROOT="$R" BENCH_EXTRA_PATH="$BIN:$(dirname "$NEXTFLOW")" \
    BENCH_IO_DIR="$T/io" BENCH_DISK_STAT="$R/sys/block/sdb/stat" BENCH_SKIP_GIT=1 \
    NXF_HOME="${NXF_HOME:-$HOME/.nextflow}" NXF_VER=26.04.4 bash "$BENCH/run.sh" pilot < /dev/null 2>&1)"
check "pilot STARTED" out_has "^STARTED: pilot session"
check "pilot ends" wait_state done 900
rm -f "$CTL/stress_secs"
pa() { tar -xOzf "$PA" "pilot/$1" 2>/dev/null; }
check "summary: DONE" first_line_is DONE
check "probes ran first, then the repeats, then the rehearsal" \
    bash -c "[[ \"\$(tar -xOzf '$PA' pilot/progress.txt | awk '{ print \$2 }' | head -n 9 | tr '\n' ' ')\" == 'probe_idle probe_ops_semantics probe_cpu_t8 probe_mem_8g probe_mem_1g probe_io_seq probe_cooldown rep1_cpu_t8 rep1_mem_8g ' ]]"
check "calibration report written" bash -c "tar -xOzf '$PA' pilot/calibration_report.txt | grep -q '^RESULT: calibration.env written'"
check "calibration.env of the pilot marked CALIBRATED" bash -c "tar -xOzf '$PA' pilot/calibration.env | head -n 1 | grep -q '^# CALIBRATED by the pilot'"
check "the copy from before is kept" bash -c "tar -tzf '$PA' | grep -qx 'pilot/calibration_before_pilot.env'"
check "stress-ng counting found: total" bash -c "tar -xOzf '$PA' pilot/calibration.env | grep -qx 'OPS_SEMANTICS=total'"
cpu_ops="$(pa calibration.env | sed -n 's/^CPU_OPS=//p')"
check "CPU_OPS measured (${cpu_ops:-none}, was 8000)" bash -c "[[ -n '$cpu_ops' && '$cpu_ops' != 8000 ]]"
check "the rehearsal used the calibrated CPU_OPS" bash -c "tar -xOzf '$PA' pilot/specs/r1_cpu_t8.json | grep -q '\"ops\": $cpu_ops'"
check "pilot report written" bash -c "tar -xOzf '$PA' pilot/pilot_report.txt | grep -q '^PILOT REPORT'"
check "recovery test 1 passed (failed attempt retried)" bash -c "tar -xOzf '$PA' pilot/pilot_report.txt | grep -q 'injected failed attempt (r1_cpu_t2): PASSED'"
check "recovery test 2 passed (killed Nextflow call, session went on)" bash -c "tar -xOzf '$PA' pilot/pilot_report.txt | grep -q 'killed Nextflow call (r1_cpu_t8): PASSED'"
check "the killed run became valid in the repair pass" bash -c "tar -xOzf '$PA' pilot/progress.txt | grep -q 'r1_cpu_t8 VALID round=1 pass=repair'"
check "real perf test output from the start check in the archive" bash -c "tar -tzf '$PA' | grep -qx 'pilot/preflight/perf_interval_summary.csv'"
check "no processes left" no_leftovers
perf_from="$(pa results_all.csv | python3 -I -c 'import csv, sys
print(" ".join(f"{r[\"run_id\"]}:{r[\"perf_source\"]}" for r in csv.DictReader(sys.stdin) if r["perf_source"] not in ("summary", "")))')"
echo "    (runs whose perf totals did not come from the summary lines: ${perf_from:-none})"
if (( FAIL > f0 )); then
    show
    pa pilot_report.txt | sed 's/^/    report: /' | tail -n 30
    cp "$PA" /tmp/pilot_test_archive.tar.gz && echo "    archive kept: /tmp/pilot_test_archive.tar.gz (WSL)"
fi

echo "S9. nothing read stdin or had a terminal"
check "no violations recorded" test ! -s "$CTL/violations"

echo
echo "passed: $PASS   failed: $FAIL"
(( FAIL == 0 ))
