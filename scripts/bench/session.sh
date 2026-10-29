#!/usr/bin/env bash

# session.sh - one benchmark session from start to end, without any input.
#
# Started detached by run.sh (systemd-run --user, or setsid as a fallback);
# not meant to be called by hand. Usage: session.sh <step> <session_dir>
#
#   1. keep the laptop awake (systemd-inhibit, if this user may), take the
#      session lock (one session at a time), note the start
#   2. make the run specs (cases.py) on the first start, write the conditions
#   3. wait until the agent, browsers and editors are closed and the machine is
#      quiet (at most GRACE_MAX_S, then start anyway with a warning)
#   (pilot only: the probe runs, then calibrate.py computes the job sizes and
#   the run specs are made again; recovery tests are set up: one rehearsal run
#   fails once on purpose, the Nextflow call of another is killed)
#   4. all runs except the final idle runs, in schedule order, one Nextflow
#      call each (bench.nf: up to MAX_ATTEMPTS attempts); cooling break
#      (BREAK_S, temperature logged in breaks.txt) between rounds
#   5. repair pass: one more Nextflow call for every run that is still missing
#   6. cooling break, then the idle runs at the end
#   7. end check (check_session.py: all runs, no overlap, conditions; combined
#      tables results_all.csv, parts_all.csv, attempts_all.csv), summary.txt,
#      archive ~/thesis-results/thesis_<step>.tar.gz (checked), then the
#      working folder and the fio folder are deleted
# Runs that are already VALID are skipped, so after a crash or reboot the same
# start command simply continues. Without the power cable the session waits;
# when the battery reaches BATTERY_STOP_PCT it stops cleanly (status stopped)
# and the same command continues it later.

set -u
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec </dev/null

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STEP="$1"
SESSION="$2"
RESULTS="$(dirname "$SESSION")"
exec >> "$SESSION/session.log" 2>&1
# shellcheck source=common.sh
source "$HERE/common.sh"
# shellcheck source=/dev/null
source "$SESSION/session.env"

log() {
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"
}

# ---- 1. awake, lock, state ----------------------------------------------------------------

if [[ -z "${BENCH_INHIBITED:-}" && -n "${SYSTEMD_INHIBIT:-}" ]]; then
    if "$SYSTEMD_INHIBIT" --what=sleep:idle:handle-lid-switch --who=thesis-bench --why=check --mode=block true 2>/dev/null; then
        export BENCH_INHIBITED=1
        exec "$SYSTEMD_INHIBIT" --what=sleep:idle:handle-lid-switch --who=thesis-bench \
            --why="energy measurement session" --mode=block "$BASH" "$0" "$@"
    fi
    log "sleep inhibitor not allowed for this user; the one-time setup keeps the laptop from sleeping"
fi

exec 8>>"$RESULTS/.session.lock"
if ! "$FLOCK" -n 8; then
    log "another session holds the lock; this start does nothing"
    exit 0
fi

STARTED="$(date -u +%FT%TZ)"
[[ -s "$SESSION/first_start.txt" ]] || echo "$STARTED" > "$SESSION/first_start.txt"

set_state() {   # STATUS [CURRENT]
    {
        printf 'status=%s\nstep=%s\npid=%s\ndetach=%s\nstarted=%s\nupdated=%s\ncurrent=%s\n' \
            "$1" "$STEP" "$$" "${BENCH_DETACHED:-unknown}" "$STARTED" "$(date -u +%FT%TZ)" "${2:-}"
    } > "$SESSION/state.txt.tmp" && mv -f "$SESSION/state.txt.tmp" "$SESSION/state.txt"
}

set_state running "starting"
: > "$SESSION/.started"
log "=== session $STEP started (pid $$, ${BENCH_DETACHED:-unknown}, inhibitor ${BENCH_INHIBITED:-no}) ==="

# the cgroup of this session: run_case.sh separates its CPU time from other programs
CG="$("$PYTHON" -I "$HERE/sessionlib.py" cgroup)"
if [[ -n "$CG" && -n "${CGROUP_ROOT:-}" ]]; then
    printf "CGROUP_OURS='%s'\n" "$CG" >> "$SESSION/session.env"
    log "own cgroup: $CG"
fi

stop_session() {   # REASON - stop cleanly; the same start command continues later
    log "STOPPED: $1"
    "$PYTHON" -I "$HERE/sessionlib.py" summary "$SESSION" "$STEP" --stopped "$1" > /dev/null
    set_state stopped "$1"
    exit 0
}

# ---- 2. specs and conditions ------------------------------------------------------------------

if [[ ! -e "$SESSION/specs/.complete" ]]; then
    if "$PYTHON" -I "$HERE/cases.py" specs "$SESSION/schedule.csv" "$SESSION/calibration.env" \
            "$SESSION/session.env" "$SESSION/specs"; then
        : > "$SESSION/specs/.complete"
    else
        stop_session "the run descriptions could not be made (see session.log)"
    fi
fi

conditions() {   # FILE - the machine state, for the thesis (not used by any check)
    local c
    {
        echo "time $(date -u +%FT%TZ)"
        echo "--- system"; uname -a; uptime
        echo "--- governor / EPP / profile (cpu0)"
        cat "$CPU_SYSFS/cpu0/cpufreq/scaling_governor" "$CPU_SYSFS/cpu0/cpufreq/energy_performance_preference" 2>/dev/null
        command -v powerprofilesctl > /dev/null && powerprofilesctl get 2>/dev/null
        echo "--- power cable / battery"
        [[ -n "${AC_FILE:-}" ]] && cat "$AC_FILE"; [[ -n "${BATTERY_FILE:-}" ]] && cat "$BATTERY_FILE"
        echo "--- package temperature (milli-C)"; cat "$TEMP_FILE" 2>/dev/null
        echo "--- RAPL limits"
        for c in "$RAPL_PKG_DIR" "${RAPL_MMIO_DIR:-}"; do
            [[ -n "$c" ]] && grep -H . "$c"/constraint_*_name "$c"/constraint_*_power_limit_uw 2>/dev/null
        done
        echo "--- memory"; free -m 2>/dev/null
        echo "--- disk"; df -h "$IO_DIR" "$RESULTS" 2>/dev/null
        echo "--- busiest processes"; ps -eo pid,pcpu,rss,comm --sort=-pcpu 2>/dev/null | head -n 15
        echo "--- logged-in sessions"; loginctl list-sessions --no-legend 2>/dev/null
    } > "$1" 2>&1
}
conditions "$SESSION/conditions_start_$(date -u +%Y%m%dT%H%M%SZ).txt"

# ---- 3. wait until the machine is quiet ----------------------------------------------------------

busy_cpus() {   # average busy CPUs over 5 s, from /proc/stat
    local a b
    a="$(head -n 1 "${PROC_STAT:-/proc/stat}")"
    sleep 5
    b="$(head -n 1 "${PROC_STAT:-/proc/stat}")"
    awk -v a="$a" -v b="$b" -v n="${NCPU:-1}" 'BEGIN {
        split(a, x, " "); split(b, y, " ")
        bt = (y[2]+y[3]+y[4]+y[7]+y[8]+y[9]) - (x[2]+x[3]+x[4]+x[7]+x[8]+x[9])
        tt = bt + (y[5]+y[6]) - (x[5]+x[6])
        printf "%.2f", (tt > 0 ? bt / tt * n : 0) }'
}

grace_wait() {
    local start names busy n
    now_s; start="$NOW"
    while :; do
        names=""
        for n in ${WAIT_FOR_GONE:-}; do
            pgrep -x "$n" > /dev/null 2>&1 && names+="$n "
        done
        busy="$(busy_cpus)"
        now_s
        printf '%s waited %ss; still running: [%s]; busy CPUs %s\n' "$(date -u +%FT%TZ)" "$((NOW - start))" \
            "${names% }" "$busy" >> "$SESSION/waits.txt"
        if [[ -z "$names" ]] && awk -v b="$busy" -v m="$BG_CPU_MAX" 'BEGIN { exit !(b <= m) }'; then
            log "machine quiet after $((NOW - start)) s"
            return 0
        fi
        if (( NOW - start >= GRACE_MAX_S )); then
            echo "still running: ${names:-nothing from the list}; busy CPUs $busy (limit $BG_CPU_MAX)" > "$SESSION/start_warning.txt"
            log "WARNING: starting anyway after $((NOW - start)) s: ${names:-} busy CPUs $busy"
            return 0
        fi
        set_state running "waiting for a quiet machine: ${names:-busy CPUs $busy}"
        sleep 10
    done
}

# ---- 4-6. runs --------------------------------------------------------------------------------

power_guard() {   # wait while the power cable is out; stop cleanly when the battery is low
    local ac cap
    [[ -n "${AC_FILE:-}" ]] || return 0
    rd ac "$AC_FILE"
    [[ "$ac" == 1 ]] && return 0
    log "power cable out: waiting"
    set_state waiting_for_power
    while :; do
        rd ac "$AC_FILE"
        if [[ "$ac" == 1 ]]; then
            log "power cable back"
            set_state running
            return 0
        fi
        if [[ -n "${BATTERY_FILE:-}" ]]; then
            rd cap "$BATTERY_FILE"
            if [[ "$cap" =~ ^[0-9]+$ ]] && (( cap <= BATTERY_STOP_PCT )); then
                stop_session "no power cable and the battery is at $cap %"
            fi
        fi
        sleep "$POWER_POLL_S"
    done
}

cooling_break() {   # WHY
    local t end left
    log "cooling break of $BREAK_S s ($1)"
    set_state running "cooling break ($1)"
    now_s; end=$((NOW + BREAK_S))
    while now_s; (( NOW < end )); do
        rd t "$TEMP_FILE"
        printf '%s %s: package %s milli-C\n' "$(date -u +%FT%TZ)" "$1" "${t:--}" >> "$SESSION/breaks.txt"
        power_guard
        left=$((end - NOW))
        sleep $(( left < 10 ? left : 10 ))
    done
    rd t "$TEMP_FILE"
    printf '%s %s: end of break, package %s milli-C\n' "$(date -u +%FT%TZ)" "$1" "${t:--}" >> "$SESSION/breaks.txt"
}

LAST_ROUND=""          # round of the last run of this process ('-' = idle run)

run_one() {   # RUN_ID PHASE ROUND PASS
    local run_id="$1" phase="$2" rnd="$3" pass="$4" spec="$SESSION/specs/$1.json" to rc k t0 result pid watcher=""
    [[ -e "$SESSION/runs/$run_id/VALID" ]] && return 0
    if [[ -n "$LAST_ROUND" && "$LAST_ROUND" != "-" ]] \
            && { [[ "$phase" == round && "$rnd" != "$LAST_ROUND" ]] || [[ "$phase" == idle_end ]]; }; then
        cooling_break "after round $LAST_ROUND"
    fi
    power_guard
    set_state running "$run_id ($pass)"
    to="$("$PYTHON" -I "$HERE/sessionlib.py" timeout "$spec" "$MAX_ATTEMPTS")" || to=7200
    mkdir -p "$SESSION/runs/$run_id" "$SESSION/nextflow"
    if [[ "$(cat "$SESSION/.inject_kill" 2>/dev/null)" == "$run_id" ]]; then
        rm -f "$SESSION/.inject_kill"
        kill_nextflow_during "$run_id" 8>&- &   # pilot recovery test; must not hold the session lock
        watcher=$!
    fi
    t0="$EPOCHREALTIME"
    (
        cd "$SESSION/nextflow" || exit 99
        "$TIMEOUT_BIN" -k 60 "$to" env -i HOME="$HOME" PATH="$JAVA_HOME/bin:/usr/bin:/bin" JAVA_HOME="$JAVA_HOME" \
            NXF_HOME="$NXF_HOME" NXF_VER="$NXF_VER" NXF_OFFLINE=true NXF_DISABLE_CHECK_LATEST=true \
            "$NEXTFLOW" -C "$REPO/workflows/bench.config" run "$REPO/workflows/bench.nf" -ansi-log false \
            -work-dir "$SESSION/nextflow/work" --spec "$spec" --session_env "$SESSION/session.env" \
            --runs_dir "$SESSION/runs" --max_attempts "$MAX_ATTEMPTS"
    ) < /dev/null >> "$SESSION/nextflow/stdout.txt" 2>&1
    rc=$?
    [[ -n "$watcher" ]] && stop_pid "$watcher"
    if (( rc == 124 || rc == 137 )); then
        log "Nextflow call for $run_id stopped after $to s"
        for pid in $(pgrep -f -- "$SESSION/runs/$run_id/a" 2>/dev/null); do
            kill_tree "$pid"
            kill "$pid" 2>/dev/null
        done
    fi
    k=1
    while [[ -e "$SESSION/runs/$run_id/nextflow_call$k.log" ]]; do k=$((k + 1)); done
    cp "$SESSION/nextflow/.nextflow.log" "$SESSION/runs/$run_id/nextflow_call$k.log" 2>/dev/null
    rm -rf "$SESSION/nextflow/work" "$SESSION/nextflow/.nextflow/cache"      # small, but nothing needs them
    result=MISSING
    [[ -e "$SESSION/runs/$run_id/VALID" ]] && result=VALID
    printf '%s %s %s round=%s pass=%s attempts=%s nextflow_exit=%s seconds=%s\n' "$(date -u +%FT%TZ)" "$run_id" \
        "$result" "$rnd" "$pass" "$(find "$SESSION/runs/$run_id" -maxdepth 1 -name 'a[0-9]*' | wc -l)" "$rc" \
        "$(awk -v a="$t0" -v b="$EPOCHREALTIME" 'BEGIN { printf "%.0f", b - a }')" >> "$SESSION/progress.txt"
    log "$run_id: $result (nextflow exit $rc)"
    if [[ "$phase" == round ]]; then LAST_ROUND="$rnd"; else LAST_ROUND="-"; fi
}

run_pass() {   # WHICH (sessionlib phase filter) PASS
    local run_id phase rnd
    "$PYTHON" -I "$HERE/sessionlib.py" pending "$SESSION" "$1" > "$SESSION/.pending_$2"
    while read -r run_id phase rnd <&3; do
        run_one "$run_id" "$phase" "$rnd" "$2"
    done 3< "$SESSION/.pending_$2"
    rm -f "$SESSION/.pending_$2"
}

# ---- pilot only: probes, calibration, recovery tests ------------------------------------------

has_phase() { grep -q "^[0-9]*,[^,]*,$1," "$SESSION/schedule.csv"; }

calibrate_from_probes() {   # once: job sizes from the probe runs, then new run specs
    [[ -e "$SESSION/.calibrated" ]] && return 0
    set_state running "calibration from the probe runs"
    if "$PYTHON" -I "$HERE/calibrate.py" "$SESSION" > /dev/null; then
        log "calibration written (calibration_report.txt)"
    else
        log "CALIBRATION FAILED: the rest of the pilot uses the old values (see calibration_report.txt)"
    fi
    rm -f "$SESSION/specs/.complete"
    if "$PYTHON" -I "$HERE/cases.py" specs "$SESSION/schedule.csv" "$SESSION/calibration.env" \
            "$SESSION/session.env" "$SESSION/specs"; then
        : > "$SESSION/specs/.complete"
        : > "$SESSION/.calibrated"
    else
        stop_session "the run descriptions could not be made after the calibration (see session.log)"
    fi
}

# Two recovery tests on the real laptop: the first rehearsal run fails once on
# purpose (run_case.sh sees the marker), and the Nextflow call of the third
# rehearsal run is killed during its window (as if Nextflow crashed). Both runs
# must become valid later and the session must go on.
setup_recovery_tests() {
    local fail_id kill_id
    [[ -e "$SESSION/recovery_test.txt" ]] && return 0
    fail_id="$("$PYTHON" -I "$HERE/sessionlib.py" pending "$SESSION" only:round | sed -n 1p | cut -d' ' -f1)"
    kill_id="$("$PYTHON" -I "$HERE/sessionlib.py" pending "$SESSION" only:round | sed -n 3p | cut -d' ' -f1)"
    [[ -n "$fail_id" ]] && : > "$SESSION/.inject_failure_$fail_id"
    [[ -n "$kill_id" ]] && echo "$kill_id" > "$SESSION/.inject_kill"
    printf 'fail=%s\nkill=%s\n' "$fail_id" "$kill_id" > "$SESSION/recovery_test.txt"
    log "recovery tests: $fail_id fails once on purpose; the Nextflow call of $kill_id is killed during its window"
}

kill_nextflow_during() {   # RUN_ID - runs in the background
    local id="$1" _
    for _ in $(seq 1 1800); do
        if grep -qs '^win_start' "$SESSION/runs/$id"/a*/window.txt; then
            sleep 3
            printf 'killed_nextflow %s %s\n' "$id" "$(date -u +%FT%TZ)" >> "$SESSION/recovery_test.txt"
            log "recovery test: killing the Nextflow call of $id"
            pkill -KILL -f -- "--spec $SESSION/specs/$id.json"
            return 0
        fi
        sleep 1
    done
}

# ---- 7. end ----------------------------------------------------------------------------------------

make_archive() {
    local archive="$RESULTS/thesis_${STEP}.tar.gz" items=() x
    for x in summary.txt check.txt pilot_report.txt results_all.csv parts_all.csv attempts_all.csv preflight.txt \
             preflight session.env schedule.csv calibration.env calibration_before_pilot.env calibration_report.txt \
             recovery_test.txt git.txt first_start.txt specs runs progress.txt breaks.txt waits.txt \
             start_warning.txt session.log nextflow/stdout.txt; do
        [[ -e "$SESSION/$x" ]] && items+=("$STEP/$x")
    done
    for x in "$SESSION"/conditions_*.txt; do
        [[ -e "$x" ]] && items+=("$STEP/${x##*/}")
    done
    "$TAR" -czf "$archive.part" -C "$RESULTS" "${items[@]}" \
        && "$TAR" -tzf "$archive.part" > "$SESSION/.archive_list" \
        && grep -qx "$STEP/summary.txt" "$SESSION/.archive_list" \
        && mv -f "$archive.part" "$archive" \
        && (cd "$RESULTS" && "$SHA256SUM" "${archive##*/}" > "${archive##*/}.sha256") \
        && cp "$SESSION/summary.txt" "$RESULTS/thesis_${STEP}_summary.txt"
}

finish_session() {
    log "all passes done; end check, summary and archive"
    set_state running "end check, summary and archive"
    "$PYTHON" -I "$HERE/check_session.py" "$SESSION" "$STEP" > /dev/null \
        || log "end check: FAILED or could not run (see check.txt)"
    if [[ "$STEP" == pilot ]]; then
        "$PYTHON" -I "$HERE/pilot_report.py" "$SESSION" > /dev/null || log "pilot report could not be written"
    fi
    "$PYTHON" -I "$HERE/sessionlib.py" summary "$SESSION" "$STEP" --final > /dev/null
    conditions "$SESSION/conditions_end.txt"
    if ! make_archive; then
        rm -f "$RESULTS/thesis_${STEP}.tar.gz.part"
        log "ARCHIVE FAILED: all data stays in $SESSION"
        set_state done_without_archive "archive failed; data kept in $SESSION"
        exit 0
    fi
    log "archive $RESULTS/thesis_${STEP}.tar.gz written and checked; deleting the working files"
    rm -rf "$IO_DIR"
    find "$SESSION" -mindepth 1 -maxdepth 1 ! -name summary.txt ! -name state.txt -exec rm -rf {} +
    set_state done
}

grace_wait
set_state running
if has_phase probe; then                       # pilot: probes, then the job sizes
    run_pass only:probe probe
    run_pass only:probe probe_repair
    calibrate_from_probes
fi
[[ "$STEP" == pilot ]] && setup_recovery_tests
run_pass except:probe,idle_end main
run_pass except:probe,idle_end repair
run_pass only:idle_end end
finish_session
exit 0
