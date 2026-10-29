#!/usr/bin/env python3

"""
sessionlib.py - small helpers for session.sh and run.sh (standard library only)

  pending  <session> <all | only:<phase>,... | except:<phase>,...>
           runs without a VALID marker, in schedule order: "<run_id> <phase> <round or ->"
           (phases: probe, repeat (pilot only), idle_start, round, idle_end)
  timeout  <spec.json> <max_attempts>
           seconds after which one Nextflow call for this run is stopped (far
           above the normal time: watchdog, waits and retries included)
  estimate <session>        rough seconds the open runs still need
  cgroup                    this process's cgroup v2 folder (from /proc/self/cgroup)
  summary  <session> <step> [--stopped REASON | --final]
           writes <session>/summary.txt; first line DONE, MISSING: ..., CHECK FAILED: ...
           (both joined with '; ' when both apply) or STOPPED: ...; --final = end of
           the session: the end check (check.txt) must exist
  status   <results> [step] what run.sh prints while a session runs or after it
"""

import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import benchlib  # noqa: E402
import cases  # noqa: E402

SYSROOT = os.environ.get("BENCH_SYSROOT", "")
STEPS = ("pilot", "main")


def rows_of(session):
    return cases.read_schedule(os.path.join(session, "schedule.csv"))


def valid(session, run_id):
    return os.path.exists(os.path.join(session, "runs", run_id, "VALID"))


def attempts(session, run_id):
    d = os.path.join(session, "runs", run_id)
    try:
        return sorted((n for n in os.listdir(d) if n.startswith("a") and n[1:].isdigit()), key=lambda n: int(n[1:]))
    except OSError:
        return []


def read_state(session):
    state = {}
    for line in benchlib.read_text(os.path.join(session, "state.txt")).splitlines():
        k, _, v = line.partition("=")
        if k:
            state[k] = v
    return state


def phase_filter(which):
    """'all', 'only:<phase>,...' or 'except:<phase>,...' -> function(phase) -> bool"""
    if which == "all":
        return lambda phase: True
    kind, _, names = which.partition(":")
    names = set(names.split(","))
    if kind == "only":
        return lambda phase: phase in names
    if kind == "except":
        return lambda phase: phase not in names
    raise ValueError(f"bad phase filter {which!r}")


def cmd_pending(session, which):
    keep = phase_filter(which)
    for r in rows_of(session):
        if keep(r["phase"]) and not valid(session, r["run_id"]):
            print(r["run_id"], r["phase"], r["round"] or "-")
    return 0


def call_timeout(spec_path, max_attempts):
    spec = benchlib.load_spec(spec_path)
    expected = float(spec["expected_s"])
    per_attempt = benchlib.watchdog_seconds(expected, {}) + 300 + 600 + 120   # watchdog, settle, power cable, start-up
    return int(max_attempts) * per_attempt + 300


def estimate(session):
    """Rough seconds for the open runs: expected run time + wait + Nextflow start, plus the breaks."""
    cal = cases.load_calibration(os.path.join(session, "calibration.env"))
    open_rows = [r for r in rows_of(session) if not valid(session, r["run_id"])]
    secs = sum(cases.CASE_BY_ID[r["case_id"]]["expected"](cal) + float(cal["SETTLE_EST_S"]) + float(cal["NXF_START_S"])
               for r in open_rows)
    rounds = {r["round"] for r in open_rows if r["phase"] == "round"}
    breaks = len(rounds) + (1 if any(r["phase"] == "idle_end" for r in open_rows) else 0)
    return secs + max(0, breaks - 1) * float(cal["BREAK_S"])


def cgroup_dir():
    """cgroup v2 folder of this process, e.g. /sys/fs/cgroup/user.slice/.../thesis-bench-main.service"""
    for line in benchlib.read_text(SYSROOT + "/proc/self/cgroup").splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
            return SYSROOT + "/sys/fs/cgroup" + (rel if rel != "/" else "")
    return ""


def flags_of(session, run_id):
    """Soft flags of the valid attempt of a run (from its verdict.txt)."""
    att = benchlib.read_text(os.path.join(session, "runs", run_id, "VALID")).strip()
    for line in benchlib.read_text(os.path.join(session, "runs", run_id, att, "verdict.txt")).splitlines():
        if line.startswith("soft:"):
            return [f.strip() for f in line[5:].split(",") if f.strip() and f.strip() != "-"]
    return []


def last_hard_reason(session, run_id):
    att = attempts(session, run_id)
    if not att:
        return "never started"
    for line in benchlib.read_text(os.path.join(session, "runs", run_id, att[-1], "verdict.txt")).splitlines():
        if line.startswith("hard:"):
            return line[5:].strip()
    return "no verdict"


def check_result(session):
    """(problem topics of check.txt other than 'complete', report text), or (None, '') without check.txt."""
    text = benchlib.read_text(os.path.join(session, "check.txt"))
    if not text:
        return None, ""
    topics = []
    for line in text.splitlines():
        if line.startswith("RESULT: FAILED:"):
            topics = [t.strip() for t in line[len("RESULT: FAILED:"):].split(",") if t.strip() and t.strip() != "complete"]
    return topics, text


def summary_text(session, step, stopped=None, final=False):
    """final=True: the end of a session, where check_session.py must have run (check.txt)."""
    rows = rows_of(session)
    ok = [r for r in rows if valid(session, r["run_id"])]
    missing = [r for r in rows if not valid(session, r["run_id"])]
    check_topics, check_text = check_result(session)
    if stopped:
        first = f"STOPPED: {stopped}. Start the same command again to continue ({len(missing)} runs still open)."
    else:
        parts = []
        if missing:
            parts.append(f"MISSING: {len(missing)} of {len(rows)} runs: " + " ".join(r["run_id"] for r in missing[:30])
                         + (" ..." if len(missing) > 30 else ""))
        if check_topics:
            parts.append(f"CHECK FAILED: {', '.join(check_topics)} (see check.txt)")
        elif final and check_topics is None:
            parts.append("CHECK FAILED: the end check did not run (see session.log)")
        first = "; ".join(parts) or "DONE"
    state = read_state(session)
    lines = [first, "",
             f"step: {step}",
             f"first start: {benchlib.read_text(os.path.join(session, 'first_start.txt')).strip() or '?'}",
             f"summary written: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"started detached by: {state.get('detach', '?')}",
             f"runs valid: {len(ok)} of {len(rows)}"]
    n_att = {r["run_id"]: len(attempts(session, r["run_id"])) for r in rows}
    retried = [rid for rid, n in n_att.items() if n > 1]
    lines.append(f"attempts: {sum(n_att.values())} in total; {len(retried)} runs needed more than one")
    counts = {}
    for r in ok:
        for f in flags_of(session, r["run_id"]):
            key = f.split(":")[0]
            counts.setdefault(key, []).append(r["run_id"])
    if counts:
        lines.append("flags on valid runs (kept, see verdict.txt of the run):")
        for k, ids in sorted(counts.items()):
            lines.append(f"  {k}: {len(ids)} run(s): " + " ".join(ids[:10]) + (" ..." if len(ids) > 10 else ""))
    if missing:
        lines.append("why the missing runs failed (last attempt):")
        for r in missing[:30]:
            lines.append(f"  {r['run_id']}: {last_hard_reason(session, r['run_id'])}")
    warn = [l for l in benchlib.read_text(os.path.join(session, "preflight.txt")).splitlines() if l.startswith("WARN")]
    start_warning = benchlib.read_text(os.path.join(session, "start_warning.txt")).strip()
    if warn or start_warning:
        lines.append("warnings:")
        lines += [f"  {w}" for w in warn]
        if start_warning:
            lines.append(f"  at the start: {start_warning}")
    if check_text:
        lines.append("end check (check.txt):")
        lines += [f"  {l}" for l in check_text.splitlines()[1:]]
    results = os.path.dirname(os.path.abspath(session))
    lines.append(f"archive: {os.path.join(results, f'thesis_{step}.tar.gz')}")
    return "\n".join(lines) + "\n"


def cmd_summary(session, step, stopped=None, final=False):
    text = summary_text(session, step, stopped, final)
    with open(os.path.join(session, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")
    return 0


def status_text(results, only=None):
    out = []
    for step in STEPS:
        if only and step != only:
            continue
        session = os.path.join(results, step)
        if not os.path.isdir(session):
            continue
        state = read_state(session)
        st = state.get("status", "not started")
        archive = os.path.join(results, f"thesis_{step}.tar.gz")
        if st == "done":
            first = benchlib.read_text(os.path.join(session, "summary.txt")).splitlines()[:1]
            out.append(f"{step}: {first[0] if first else 'DONE'}")
            if os.path.exists(archive):
                out.append(f"  send this file: {archive}")
            continue
        line = f"{step}: {st.upper()}"
        if state.get("updated"):
            line += f" (last update {state['updated']}"
            line += f", now: {state['current']})" if state.get("current") else ")"
        out.append(line)
        if os.path.exists(os.path.join(session, "schedule.csv")):
            rows = rows_of(session)
            n_ok = sum(valid(session, r["run_id"]) for r in rows)
            out.append(f"  runs valid: {n_ok} of {len(rows)}")
            if st in ("running", "waiting_for_power") and os.path.exists(os.path.join(session, "calibration.env")):
                try:
                    left = estimate(session)
                    end = datetime.now().timestamp() + left
                    out.append(f"  still about {left / 3600:.1f} h (rough estimate), ends about "
                               f"{datetime.fromtimestamp(end).strftime('%a %H:%M')} local time")
                except (OSError, ValueError, KeyError):
                    pass
        if st == "stopped":
            first = benchlib.read_text(os.path.join(session, "summary.txt")).splitlines()[:1]
            if first:
                out.append(f"  {first[0]}")
    return "\n".join(out) + "\n" if out else "no session yet\n"


def main(argv):
    try:
        if len(argv) == 4 and argv[1] == "pending":
            return cmd_pending(argv[2], argv[3])
        if len(argv) == 4 and argv[1] == "timeout":
            print(call_timeout(argv[2], argv[3]))
            return 0
        if len(argv) == 3 and argv[1] == "estimate":
            print(int(estimate(argv[2])))
            return 0
        if len(argv) == 2 and argv[1] == "cgroup":
            print(cgroup_dir())
            return 0
        if len(argv) == 4 and argv[1] == "summary":
            return cmd_summary(argv[2], argv[3])
        if len(argv) == 5 and argv[1] == "summary" and argv[4] == "--final":
            return cmd_summary(argv[2], argv[3], final=True)
        if len(argv) == 6 and argv[1] == "summary" and argv[4] == "--stopped":
            return cmd_summary(argv[2], argv[3], argv[5])
        if len(argv) in (3, 4) and argv[1] == "status":
            print(status_text(argv[2], argv[3] if len(argv) == 4 else None), end="")
            return 0
    except (OSError, ValueError, KeyError) as e:
        print(f"sessionlib: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
