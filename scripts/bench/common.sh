# common.sh - small helpers shared by run_case.sh, window.sh and sampler.sh.
# Sourced, not executed. Pure bash plus ps/awk; nothing here reads stdin.

# rd VAR FILE - first line of FILE into VAR ("" if FILE is missing or unreadable).
rd() {
    local -n _rd_var="$1"
    _rd_var=""
    { IFS= read -r _rd_var < "$2"; } 2>/dev/null
    return 0
}

# event NAME [ARGS] - one line in $OUT/events.txt (read by benchlib.py finish).
event() {
    printf '%s\n' "$*" >> "$OUT/events.txt"
}

# now_s - whole seconds since the epoch into NOW, without starting a process.
now_s() {
    NOW="${EPOCHREALTIME%[.,]*}"
}

# descendants ROOT [EXCLUDE] - pids of all descendants of ROOT, leaving out
# EXCLUDE and everything below it.
descendants() {
    ps -e -o pid= -o ppid= | awk -v root="$1" -v excl="${2:-0}" '
        { par[$1] = $2 }
        END {
            for (p in par) {
                if (p == root || p == excl) continue
                q = par[p]; n = 0
                while (q != "" && q > 1 && n < 1000) {
                    if (q == excl) break
                    if (q == root) { print p; break }
                    q = (q in par) ? par[q] : ""
                    n++
                }
            }
        }'
}

# stop_pid PID - stop a helper loop (watchdog, sampler) started with &: the
# loop itself first, so it cannot act on the death of its `sleep`, then its
# children. Background jobs of a script ignore SIGINT, so this uses SIGTERM.
stop_pid() {
    local kids
    kids="$(descendants "$1")"
    kill "$1" 2>/dev/null
    # shellcheck disable=SC2086
    [[ -n "$kids" ]] && kill $kids 2>/dev/null
    wait "$1" 2>/dev/null
    return 0
}

# kill_tree ROOT [EXCLUDE] - stop every descendant of ROOT (not ROOT itself).
# SIGINT first, so stress-ng and fio stop cleanly and still print their
# counters (if they did not inherit an ignored SIGINT from a background
# job), then SIGTERM, then SIGKILL for anything left.
kill_tree() {
    local root="$1" excl="${2:-}" sig pids _
    for sig in INT TERM KILL; do
        pids="$(descendants "$root" "$excl")"
        [[ -z "$pids" ]] && return 0
        # shellcheck disable=SC2086
        kill -s "$sig" $pids 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            sleep 0.2
            pids="$(descendants "$root" "$excl")"
            [[ -z "$pids" ]] && return 0
        done
    done
}
