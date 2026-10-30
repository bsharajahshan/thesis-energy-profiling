#!/usr/bin/env bash

# run.sh - the ONE command on the measurement laptop.
#
#   bash scripts/bench/run.sh pilot     start (or continue) the pilot
#   bash scripts/bench/run.sh memcheck  start (or continue) the memory check (after the pilot)
#   bash scripts/bench/run.sh main      start (or continue) the main session
#   bash scripts/bench/run.sh status    how far the sessions are
#   bash scripts/bench/run.sh clean     delete everything the benchmark wrote
#                                       (only after the archive has been sent)
#
# pilot / main: checks the laptop (preflight.py) and, if everything is fine,
# starts the session in the background, detached from this shell
# (systemd-run --user; setsid if that is not possible), and returns within
# seconds with "STARTED". It never asks anything and needs no sudo. While the
# session runs, the same command only shows the progress; after a crash or a
# reboot it continues the session; after the end it shows the summary and the
# archive to send: ~/thesis-results/thesis_<step>.tar.gz
#
# Exit 0 = started, running or done; 1 = not started (the problems are
# listed above that line); 2 = wrong usage.
# Test hooks (environment): BENCH_RESULTS, BENCH_SCHEDULE, BENCH_CALIBRATION,
# BENCH_PYTHON, BENCH_NO_SYSTEMD, plus those of preflight.py.

set -u
export LC_ALL=C
exec </dev/null

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
RESULTS="${BENCH_RESULTS:-$HOME/thesis-results}"

PY="${BENCH_PYTHON:-}"
for p in /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [[ -z "$PY" && -n "$p" && -x "$p" ]] && PY="$p"
done
FLOCK="$(command -v flock 2>/dev/null || echo /usr/bin/flock)"

usage() {
    echo "usage: bash scripts/bench/run.sh pilot | memcheck | main | status | clean"
}

session_running() {   # the session holds $RESULTS/.session.lock for its whole life
    [[ -e "$RESULTS/.session.lock" ]] || return 1
    ! "$FLOCK" -n "$RESULTS/.session.lock" true
}

status() {
    "$PY" -I "$HERE/sessionlib.py" status "$RESULTS" "${1:-}"
}

clean() {
    local io removed=()
    if session_running; then
        echo "NOT CLEANED: a session is running. Wait until it is done."
        return 1
    fi
    if [[ -d "$RESULTS" && ! -e "$RESULTS/.thesis-bench" ]]; then
        echo "NOT CLEANED: $RESULTS was not made by this benchmark (no .thesis-bench marker)."
        return 1
    fi
    for io in /tmp/thesis-bench-io $(grep -ho "^IO_DIR=.*" "$RESULTS"/*/session.env 2>/dev/null | cut -d= -f2- | tr -d "'"); do
        # only folders the benchmark itself chose: /tmp/thesis-bench-io or inside the results folder
        if [[ -d "$io" && ( "${io##*/}" == thesis-bench-io || "$io" == "$RESULTS"/* ) ]]; then
            rm -rf "$io" && removed+=("$io")
        fi
    done
    if [[ -d "$RESULTS" ]]; then
        echo "size before cleaning: $(du -sh "$RESULTS" 2>/dev/null | cut -f1)"
        rm -rf "$RESULTS" && removed+=("$RESULTS")
    fi
    if (( ${#removed[@]} )); then
        echo "CLEANED: removed ${removed[*]}"
    else
        echo "CLEANED: nothing to remove"
    fi
    return 0
}

start_detached() {   # STEP SESSION -> DETACH (systemd | setsid)
    local unit="thesis-bench-$1"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$XDG_RUNTIME_DIR/bus" ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    fi
    if [[ -z "${BENCH_NO_SYSTEMD:-}" ]] && command -v systemd-run > /dev/null 2>&1; then
        systemctl --user reset-failed "$unit" > /dev/null 2>&1
        if systemd-run --user --quiet --collect --unit "$unit" --setenv=BENCH_DETACHED=systemd \
                /bin/bash "$HERE/session.sh" "$1" "$2" >> "$2/start.log" 2>&1; then
            DETACH=systemd
            return 0
        fi
        echo "systemd-run --user did not work (see $2/start.log); using setsid instead" >> "$2/start.log"
    fi
    BENCH_DETACHED=setsid setsid -f /bin/bash "$HERE/session.sh" "$1" "$2" < /dev/null >> "$2/start.log" 2>&1
    DETACH=setsid
}

if [[ -z "$PY" ]]; then
    echo "NOT STARTED: python3 not found (it is part of every Ubuntu installation)."
    exit 1
fi

case "${1:-}" in
    status) status; exit 0 ;;
    clean) clean; exit $? ;;
    pilot|main|memcheck) STEP="$1" ;;
    *) usage >&2; exit 2 ;;
esac

SESSION="$RESULTS/$STEP"
if ! mkdir -p "$SESSION"; then
    echo "NOT STARTED: cannot create $SESSION"
    exit 1
fi
: > "$RESULTS/.thesis-bench"

if session_running; then
    echo "RUNNING: a session is already running; nothing new was started."
    status
    exit 0
fi
if grep -qs '^status=done' "$SESSION/state.txt"; then
    echo "DONE: this session has finished."
    cat "$SESSION/summary.txt" 2>/dev/null
    exit 0
fi

SCHEDULE="${BENCH_SCHEDULE:-$HERE/schedules/$STEP.csv}"
CALIBRATION="${BENCH_CALIBRATION:-$HERE/calibration.env}"
if [[ -e "$SESSION/state.txt" ]]; then
    echo "Continuing the $STEP session (runs already done are kept). Checking the laptop ..."
else
    echo "Starting the $STEP session. Checking the laptop ..."
fi
"$PY" -I "$HERE/preflight.py" --step "$STEP" --repo "$REPO" --results "$RESULTS" --session "$SESSION" \
    --schedule "$SCHEDULE" --calibration "$CALIBRATION"
if (( $? != 0 )); then
    echo
    echo "NOT STARTED: fix the FAIL lines above, then run the same command again."
    exit 1
fi

rm -f "$SESSION/.started"
start_detached "$STEP" "$SESSION"
for _ in $(seq 1 60); do
    [[ -e "$SESSION/.started" ]] && break
    sleep 1
done
if [[ ! -e "$SESSION/.started" ]]; then
    echo
    echo "NOT STARTED: the session did not come up within 60 s. Last lines of its logs:"
    tail -n 15 "$SESSION/start.log" "$SESSION/session.log" 2>/dev/null
    exit 1
fi

LEFT="$("$PY" -I "$HERE/sessionlib.py" estimate "$SESSION" 2>/dev/null || echo 0)"
echo
echo "STARTED: $STEP session running in the background ($DETACH)."
echo "  About $(awk -v s="$LEFT" 'BEGIN { printf "%.1f", s / 3600 }') hours (rough estimate), until about $(date -d "@$(( $(date +%s) + LEFT ))" '+%a %H:%M')."
echo "  Progress: bash $HERE/run.sh status"
echo "  Result when done: $RESULTS/thesis_${STEP}.tar.gz"
echo "  Now close the agent and all other programs; leave the laptop plugged in and alone."
exit 0
