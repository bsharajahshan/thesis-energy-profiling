#!/usr/bin/env bash

# run_case.sh - one measured run of the fixed-work benchmark, start to end.
#
# Usage: run_case.sh <session.env> <spec.json> <out_dir> [attempt=1] [max_attempts=1]
#
#   1. benchlib.py plan    -> part scripts, plan.json, plan.sh
#   2. before the window   governor and EPP as planned, 1 s sampler on (logging
#                          runs only), free disk checked (room for the fio file
#                          plus DISK_KEEP_FREE_KB, else no file is created and
#                          the run is retried later), fio test file created,
#                          then wait until the power cable is in, memory is
#                          free and the package has cooled down (settle.txt)
#   3. window.sh           the measured window, wrapped in `perf stat -a` for
#                          logging runs (if perf cannot start, the window runs
#                          without it and the run is flagged)
#   4. after the window    fio file deleted, sampler stopped, then
#                          benchlib.py finish -> result.csv, parts.csv, verdict.txt
#
# Exit 0 = valid run (also writes <out_dir>/../VALID with the attempt folder
# name, the "done" marker for the session driver), 3 = invalid run, retry it.
# Nothing here waits for input (stdin is /dev/null), asks for a password or
# depends on PATH: every tool comes as an absolute path from session.env.

set -u
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec </dev/null

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (( $# < 3 )); then
    echo "usage: run_case.sh <session.env> <spec.json> <out_dir> [attempt] [max_attempts]" >&2
    exit 3
fi
SESSION_ENV="$1"
SPEC="$2"
OUT="$3"
ATTEMPT="${4:-1}"
MAX_ATTEMPTS="${5:-1}"
mkdir -p "$OUT" || exit 3
OUT="$(cd "$OUT" && pwd)"
LOG="$OUT/run_case.log"
# shellcheck source=common.sh
source "$HERE/common.sh"

log() {
    printf '%s %s\n' "$EPOCHREALTIME" "$*" >> "$LOG"
}

# Failure before plan.json exists: benchlib finish cannot run, so write the verdict here.
fail_without_plan() {
    event "$1"
    printf 'RETRY\nhard: %s\nsoft: -\n' "$1" > "$OUT/verdict.txt"
    log "failed before planning: $1"
    printf 'run attempt %s: RETRY (%s)\n' "$ATTEMPT" "$1"
    exit 3
}

# shellcheck source=/dev/null
source "$SESSION_ENV" 2>>"$LOG" || fail_without_plan session_env_unreadable
# Pilot recovery test: session.sh leaves this marker for one run; its first
# attempt fails on purpose and must be retried. Without the marker: nothing.
if [[ -n "${SESSION_DIR:-}" && -e "$SESSION_DIR/.inject_failure_$(basename "$SPEC" .json)" ]]; then
    rm -f "$SESSION_DIR/.inject_failure_$(basename "$SPEC" .json)"
    fail_without_plan injected_failure
fi
cp "$SPEC" "$OUT/spec.json" 2>>"$LOG" || fail_without_plan spec_unreadable
"$PYTHON" -I "$HERE/benchlib.py" plan "$OUT/spec.json" "$SESSION_ENV" "$OUT" > "$OUT/plan.sh" 2>>"$LOG" \
    || fail_without_plan plan_failed
# shellcheck source=/dev/null
source "$OUT/plan.sh"
log "run $PLAN_RUN_ID attempt $ATTEMPT/$MAX_ATTEMPTS: mode $PLAN_MODE, $PLAN_NPARTS part(s), logging $PLAN_LOGGING"

SAMPLER_PID=""
WIN_PID=""
CLEANED=0

# cleanup - stop the sampler and delete the fio files; safe to call twice.
cleanup() {
    (( CLEANED )) && return 0
    CLEANED=1
    [[ -n "$SAMPLER_PID" ]] && stop_pid "$SAMPLER_PID"
    local i
    for ((i = 0; i < PLAN_NPARTS; i++)); do
        [[ -f "$OUT/clean_$i.sh" ]] && "$BASH" "$OUT/clean_$i.sh" >> "$LOG" 2>&1
    done
    return 0
}

on_signal() {
    log "stopped by a signal"
    event stopped_by_signal
    kill_tree $$
    cleanup
    "$PYTHON" -I "$HERE/benchlib.py" finish "$OUT/spec.json" "$SESSION_ENV" "$OUT" "$ATTEMPT" "$MAX_ATTEMPTS" >> "$LOG" 2>&1
    exit 3
}
trap on_signal TERM INT HUP
trap cleanup EXIT

# set_cpufreq FILE VALUE - write VALUE to every cpu*/cpufreq/FILE that differs; 1 if a write failed.
set_cpufreq() {
    local f cur bad=0
    for f in "$CPU_SYSFS"/cpu[0-9]*/cpufreq/"$1"; do
        [[ -e "$f" ]] || continue
        rd cur "$f"
        [[ "$cur" == "$2" ]] && continue
        { printf '%s' "$2" > "$f"; } 2>/dev/null || bad=1
    done
    return "$bad"
}

memavail_kb() {
    local k v u
    MEMAVAIL=0
    while read -r k v u; do
        if [[ "$k" == MemAvailable: ]]; then MEMAVAIL="$v"; return 0; fi
    done < "${PROC_MEMINFO:-/proc/meminfo}"
}

# disk_ok - is there room for the fio file AND the free space that must stay
# (DISK_KEEP_FREE_KB)? Checked before the file is created, so the run never
# fills the disk; if not, the run is not started (retried later).
disk_ok() {
    local free
    (( PLAN_NEED_DISK_KB > 0 )) || return 0
    mkdir -p "$PLAN_IO_DIR" 2>/dev/null
    free="$(df -Pk "$PLAN_IO_DIR" 2>/dev/null | awk 'NR == 2 { print $4 }')"
    if (( ${free:-0} < PLAN_NEED_DISK_KB )); then
        event "disk_low ${free:-0} ${PLAN_NEED_DISK_KB}"
        return 1
    fi
    return 0
}

# settle - wait for the power cable, free memory and a cool package.
# Returns 1 if the run cannot start (an event says why).
settle() {
    local start ac temp_mc temp_c=""
    now_s; start="$NOW"
    if [[ -n "${AC_FILE:-}" ]]; then
        while :; do
            rd ac "$AC_FILE"
            [[ "$ac" == 1 ]] && break
            now_s
            if (( NOW - start >= PLAN_AC_WAIT_MAX_S )); then event ac_timeout; return 1; fi
            sleep 2
        done
    fi
    now_s; start="$NOW"
    while :; do
        now_s
        waited=$(( NOW - start ))
        memavail_kb
        if (( MEMAVAIL < PLAN_NEED_MEM_KB )); then
            if (( waited >= PLAN_SETTLE_MAX_S )); then event "mem_low $MEMAVAIL"; return 1; fi
            sleep 2
            continue
        fi
        rd temp_mc "$TEMP_FILE"
        if [[ ! "$temp_mc" =~ ^[0-9]+$ ]]; then
            # No temperature: rest for a fixed time instead.
            [[ -e "$OUT/.temp_unreadable" ]] || { event temp_unreadable; : > "$OUT/.temp_unreadable"; }
            (( waited >= PLAN_SETTLE_FALLBACK_S )) && break
            sleep 1
            continue
        fi
        temp_c=$(( temp_mc / 1000 ))
        (( waited >= PLAN_SETTLE_MIN_S && temp_c <= PLAN_SETTLE_TEMP_C )) && break
        if (( waited >= PLAN_SETTLE_MAX_S )); then event "settle_timeout $temp_c"; break; fi
        sleep 1
    done
    now_s
    printf 'settle_s %s\ntemp_c %s\n' "$(( NOW - start ))" "${temp_c:--}" > "$OUT/settle.txt"
    return 0
}

# ---- before the window ----------------------------------------------------

if [[ -n "$PLAN_GOVERNOR" ]]; then
    set_cpufreq scaling_governor "$PLAN_GOVERNOR" || { event governor_set_failed; log "could not set governor $PLAN_GOVERNOR"; }
fi
if [[ -n "$PLAN_EPP" ]]; then
    set_cpufreq energy_performance_preference "$PLAN_EPP" || { event epp_set_failed; log "could not set EPP $PLAN_EPP"; }
fi

if [[ "$PLAN_LOGGING" == 1 ]]; then
    "$BASH" "$HERE/sampler.sh" "$SESSION_ENV" "$OUT/sampler.txt" "$$" &
    SAMPLER_PID=$!
fi

# fio files of earlier, interrupted runs (only when fio writes to a shared folder).
if [[ "$PLAN_IO_DIR" != "$OUT" ]]; then
    rm -f "$PLAN_IO_DIR"/fio_*.dat 2>/dev/null
fi

READY=1
if ! disk_ok; then
    READY=0
    log "not enough free disk for the fio file (see events.txt); no file created"
fi
for ((i = 0; READY && i < PLAN_NPARTS; i++)); do
    if [[ -f "$OUT/prep_$i.sh" ]] && ! "$BASH" "$OUT/prep_$i.sh" > "$OUT/prep_$i.out" 2>&1; then
        event "prep_failed $i"
        log "preparing part $i failed (see prep_$i.out)"
        READY=0
    fi
done
if (( READY )) && ! settle; then
    READY=0
    log "settle failed: $(tail -n 1 "$OUT/events.txt" 2>/dev/null)"
fi

# ---- the window -----------------------------------------------------------

if (( READY )); then
    WIN_CMD=("$BASH" "$HERE/window.sh" "$SESSION_ENV" "$OUT")
    if [[ "$PLAN_LOGGING" == 1 && -n "${PERF:-}" && -n "${PERF_EVENTS:-}" ]]; then
        PERF_OPTS=(-I "$PLAN_PERF_INTERVAL_MS")
        [[ "${PERF_SUMMARY:-1}" == 1 ]] && PERF_OPTS+=(--summary)   # preflight sets 0 if this perf lacks --summary
        "$PERF" stat -a -x, "${PERF_OPTS[@]}" -e "$PERF_EVENTS" \
            -o "$OUT/perf.csv" -- "${WIN_CMD[@]}" 2>> "$OUT/perf.err" &
        WIN_PID=$!
        wait "$WIN_PID"
        if ! grep -q '^win_start' "$OUT/window.txt" 2>/dev/null \
                && ! grep -q '^lock_busy' "$OUT/events.txt" 2>/dev/null; then
            log "perf did not start the window, running it without perf (see perf.err)"
            event perf_fallback
            "${WIN_CMD[@]}" &
            WIN_PID=$!
            wait "$WIN_PID"
        fi
    else
        "${WIN_CMD[@]}" &
        WIN_PID=$!
        wait "$WIN_PID"
    fi
    WIN_PID=""
fi

# ---- after the window -----------------------------------------------------

cleanup
"$PYTHON" -I "$HERE/benchlib.py" finish "$OUT/spec.json" "$SESSION_ENV" "$OUT" "$ATTEMPT" "$MAX_ATTEMPTS" >> "$LOG" 2>&1
rc=$?
if (( rc != 0 && rc != 3 )); then
    log "benchlib finish failed with exit code $rc"
    printf 'RETRY\nhard: finish_failed\nsoft: -\n' > "$OUT/verdict.txt"
    rc=3
fi
if (( rc == 0 )); then
    printf '%s\n' "${OUT##*/}" > "$(dirname "$OUT")/VALID"
fi
printf '%s attempt %s: %s\n' "$PLAN_RUN_ID" "$ATTEMPT" "$(head -n 2 "$OUT/verdict.txt" | tr '\n' ' ')"
exit "$rc"
