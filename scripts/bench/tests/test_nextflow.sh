#!/usr/bin/env bash

# test_nextflow.sh - workflows/bench.nf and bench.config with the REAL Nextflow
#
# The session driver calls Nextflow once per run, in schedule order (Nextflow
# itself does not keep the order of a list of inputs, see bench.nf). This test
# does the same.
# Part A: a stand-in run_case.sh (logs start/end, fails on request) checks the
#         Nextflow layer: schedule order kept, one run at a time, retries,
#         'ignore' after the last attempt (Nextflow still exits 0), attempt
#         folders counting on across Nextflow calls (repair pass), no config
#         warnings, no task monitor around the run, no terminal or stdin.
# Part B: the real run_case.sh on the fake laptop (fake_machine.sh) as a
#         Nextflow task, so the whole chain is checked.
# Nextflow is started the way the session will start it: no terminal
# (setsid), almost empty environment (env -i), stdin /dev/null, -C bench.config.
#
# Usage: NEXTFLOW=/path/to/nextflow NXF_HOME=/path bash scripts/bench/tests/test_nextflow.sh
#        (Java 17+; used: Nextflow 26.04.4 in WSL1 Ubuntu 22.04, Java 17)

set -u
export LC_ALL=C
NEXTFLOW="${NEXTFLOW:-nextflow}"
BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$BENCH/../.." && pwd)"
# shellcheck source=fake_machine.sh
source "$BENCH/tests/fake_machine.sh"
ROOT="$(mktemp -d /tmp/nf_test.XXXXXX)"
PASS=0
FAIL=0

finish_all() {
    fake_machine_stop
    rm -rf "$ROOT"
}
trap finish_all EXIT

ok()  { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

# nf LAUNCH_DIR ARGS... - one Nextflow call like the session driver will make; sets NF_RC, NF_SECS
nf() {
    local dir="$1" t0
    shift
    mkdir -p "$dir"
    t0=$EPOCHREALTIME
    (
        cd "$dir" || exit 99
        setsid -w env -i HOME="$HOME" PATH=/usr/bin:/bin NXF_HOME="${NXF_HOME:-$HOME/.nextflow}" \
            NXF_VER=26.04.4 NXF_OFFLINE=true NXF_DISABLE_CHECK_LATEST=true \
            "$NEXTFLOW" -C "$REPO/workflows/bench.config" run "$REPO/workflows/bench.nf" \
            -ansi-log false -work-dir "$dir/work" "$@" < /dev/null >> "$dir/nf_stdout.txt" 2>&1
    )
    NF_RC=$?
    NF_SECS=$(awk -v a="$t0" -v b="$EPOCHREALTIME" 'BEGIN { printf "%.1f", b - a }')
}

show_nf() {
    echo "    --- nextflow stdout:"; tail -n 15 "$1/nf_stdout.txt" | sed 's/^/    /'
}

no_leftovers() {
    ! pgrep -f "$BIN/" > /dev/null && ! pgrep -f "$BENCH/sampler.sh" > /dev/null
}

# ---- Part A: the Nextflow layer -------------------------------------------------------

A="$ROOT/a"
CA="$A/ctl"
L="$A/launch"                       # one launch folder for all calls, like the session folder
mkdir -p "$CA" "$A/stub" "$A/specs" "$A/runs"
touch "$CA/log" "$CA/violations"
echo r1_fail_once > "$CA/fail_once"
echo r1_fail_always > "$CA/fail_always"
cat > "$A/stub/run_case.sh" <<EOF
#!/bin/bash
# stand-in for run_case.sh: <session.env> <spec> <out_dir> <attempt> <last attempt>
CTL=$CA
EOF
cat >> "$A/stub/run_case.sh" <<'EOF'
spec="$2"; out="$3"; attempt="$4"; last="$5"
id=$(basename "$spec" .json)
if read -r -t 0.1 x; then echo "$id read stdin" >> "$CTL/violations"; fi
[[ -t 0 ]] && echo "$id has a terminal" >> "$CTL/violations"
printf 'start %s %s %s %s %s\n' "$id" "$EPOCHREALTIME" "$attempt" "$last" "${out##*/}" >> "$CTL/log"
sleep 0.5
mkdir -p "$out"
rc=0
grep -qx "$id" "$CTL/fail_always" && rc=3
if grep -qx "$id" "$CTL/fail_once" && [[ ! -e "$CTL/failed_$id" ]]; then touch "$CTL/failed_$id"; rc=3; fi
(( rc == 0 )) && echo "${out##*/}" > "$(dirname "$out")/VALID"
printf 'end %s %s %s\n' "$id" "$EPOCHREALTIME" "$rc" >> "$CTL/log"
exit $rc
EOF
ORDER=(r1_zeta r1_alpha r1_fail_once r1_mid r1_fail_always r1_last)
for id in "${ORDER[@]}"; do : > "$A/specs/$id.json"; done
touch "$A/session.env"

echo "A1. six runs in schedule order, one Nextflow call each; one fails once, one always fails"
f0=$FAIL
all_rc=0
secs=()
for id in "${ORDER[@]}"; do
    nf "$L" --spec "$A/specs/$id.json" --session_env "$A/session.env" --runs_dir "$A/runs" --bench_dir "$A/stub"
    (( NF_RC != 0 )) && all_rc=$NF_RC
    secs+=("$NF_SECS")
    [[ $id == r1_fail_always ]] && cp "$L/.nextflow.log" "$A/nextflow_fail_always.log"
done
echo "    (seconds per Nextflow call: ${secs[*]})"
check "every Nextflow call exits 0, also for the run that failed for good" test "$all_rc" -eq 0
first_order="$(awk '$1 == "start" && !seen[$2]++ { printf "%s ", $2 }' "$CA/log")"
check "runs start in schedule order" test "$first_order" == "${ORDER[*]} "
check "never two runs at once" awk '
    { t = $3 + 0 }
    $1 == "start" { if (open) bad = 1; open = 1; if (t < last_end) bad = 1 }
    $1 == "end"   { if (!open) bad = 1; open = 0; last_end = t }
    END { exit bad }' "$CA/log"
check "fail_once: attempts a1 (1 of 3) and a2 (2 of 3), right after each other" \
    test "$(awk '$1 == "start" { printf "%s/%s/%s/%s ", $2, $4, $5, $6 }' "$CA/log" | grep -o 'r1_fail_once[^ ]* r1_fail_once[^ ]*')" == "r1_fail_once/1/3/a1 r1_fail_once/2/3/a2"
check "fail_once: VALID = a2" test "$(cat "$A/runs/r1_fail_once/VALID" 2>/dev/null)" == a2
check "fail_always: three attempts, then ignored" test "$(awk '$1 == "start" && $2 == "r1_fail_always" { printf "%s/%s ", $4, $5 }' "$CA/log")" == "1/3 2/3 3/3 "
check "fail_always: no VALID marker" test ! -e "$A/runs/r1_fail_always/VALID"
check "Nextflow logged the ignored error" grep -q "Error is ignored" "$A/nextflow_fail_always.log"
check "the others ran once and are VALID" bash -c "for id in r1_zeta r1_alpha r1_mid r1_last; do [[ \$(cat '$A/runs/'\$id/VALID) == a1 ]] || exit 1; done"
check "no unrecognised config options" bash -c "! grep -qi 'unrecognized config\|unknown config' '$L/.nextflow.log' '$A/nextflow_fail_always.log'"
cmdrun="$(find "$L/work" -name .command.run | head -n 1)"
check "no task monitor (nxf_trace) around the run" bash -c "[[ -f '$cmdrun' ]] && ! grep -q 'nxf_trace\b\|nxf_mem_watch' '$cmdrun'"
(( FAIL > f0 )) && { show_nf "$L"; sed 's/^/    /' "$CA/log"; }

echo "A2. repair pass: one more Nextflow call for the missing run"
f0=$FAIL
nf "$L" --spec "$A/specs/r1_fail_always.json" --session_env "$A/session.env" --runs_dir "$A/runs" --bench_dir "$A/stub"
check "Nextflow exits 0" test "$NF_RC" -eq 0
check "attempt folders count on: a4 a5 a6 (last = 6)" test "$(awk '$1 == "start" && $2 == "r1_fail_always" { printf "%s/%s/%s ", $4, $5, $6 }' "$CA/log")" == "1/3/a1 2/3/a2 3/3/a3 4/6/a4 5/6/a5 6/6/a6 "
check "earlier attempt folders kept" test -d "$A/runs/r1_fail_always/a1"
(( FAIL > f0 )) && show_nf "$L"

echo "A3. missing argument: clear error, no task"
nf "$A/launch_bad" --session_env "$A/session.env" --runs_dir "$A/runs"
check "Nextflow exits non-zero" test "$NF_RC" -ne 0
check "message names the missing option" grep -q "needs --spec" "$A/launch_bad/nf_stdout.txt"

echo "A4. config as Nextflow reads it"
f0=$FAIL
cfg="$(cd "$A" && env -i HOME="$HOME" PATH=/usr/bin:/bin NXF_HOME="${NXF_HOME:-$HOME/.nextflow}" NXF_VER=26.04.4 NXF_OFFLINE=true \
      "$NEXTFLOW" -C "$REPO/workflows/bench.config" config -flat "$REPO/workflows/bench.nf" 2>&1 < /dev/null)"
check "executor.queueSize = 1" grep -q "executor.queueSize = 1" <<< "$cfg"
check "process.maxForks = 1" grep -q "process.maxForks = 1" <<< "$cfg"
check "trace off" grep -q "trace.enabled = false" <<< "$cfg"
check "no warnings" bash -c "! grep -qi 'warn' <<< \"\$1\"" _ "$cfg"
(( FAIL > f0 )) && sed 's/^/    /' <<< "$cfg"
check "no stdin / terminal in tasks" test ! -s "$CA/violations"

# ---- Part B: real run_case.sh as a Nextflow task -------------------------------------

echo "B. real run_case.sh on the fake laptop, started by Nextflow"
f0=$FAIL
fake_machine_start "$ROOT/fm"      # sets T=$ROOT/fm, CTL, S, BIN, $T/session.env
mkdir -p "$T/specs" "$T/runs"
cat > "$T/specs/r1_par.json" <<'EOF'
{"run_id": "r1_par", "case_id": "rq2_cpu_ram_par", "mode": "parallel", "logging": true, "governor": "powersave",
 "epp": "balance_performance", "expected_s": 1, "round": 1, "order": 1,
 "parts": [{"name": "cpu", "kind": "cpu", "threads": 8, "ops": 1000}, {"name": "ram", "kind": "vm", "vm_bytes": "1G", "ops": 50}]}
EOF
cat > "$T/specs/r1_idle.json" <<'EOF'
{"run_id": "r1_idle", "case_id": "cost_idle_nolog", "mode": "parallel", "logging": false, "governor": "powersave",
 "epp": "balance_performance", "expected_s": 1, "round": 1, "order": 2, "parts": [{"name": "idle", "kind": "idle", "seconds": 1}]}
EOF
rc_b=0
for id in r1_par r1_idle; do         # default --bench_dir: scripts/bench next to workflows/
    nf "$T/launch" --spec "$T/specs/$id.json" --session_env "$T/session.env" --runs_dir "$T/runs"
    (( NF_RC != 0 )) && rc_b=$NF_RC
done
check "Nextflow exits 0" test "$rc_b" -eq 0
check "both runs VALID" bash -c "[[ \$(cat '$T/runs/r1_par/VALID') == a1 && \$(cat '$T/runs/r1_idle/VALID') == a1 ]]"
check "result rows written" test -s "$T/runs/r1_par/a1/result.csv"
check "perf and sampler ran inside the Nextflow task" bash -c "[[ -s '$T/runs/r1_par/a1/perf.csv' && -s '$T/runs/r1_par/a1/sampler.txt' ]]"
check "nothing read stdin or had a terminal" test ! -s "$CTL/violations"
check "no processes left" no_leftovers
(( FAIL > f0 )) && { show_nf "$T/launch"; tail -n 5 "$T/runs/r1_par/a1/run_case.log" 2>/dev/null | sed 's/^/    /'; pgrep -af "$BIN/|sampler.sh" | sed 's/^/    left: /'; }

echo
echo "passed: $PASS   failed: $FAIL"
(( FAIL == 0 ))
