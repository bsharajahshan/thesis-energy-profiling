#!/usr/bin/env python3

"""
check_session.py - the complete check at the end of a session, and the combined tables

session.sh runs it before the summary and the archive; it also works on an
extracted archive on any machine:  python -I check_session.py <session folder> [step]

Checks (FAIL = the dataset is not complete or not clean):
  complete    every planned run has a valid attempt (idle start, rounds, idle end)
  overlap     no two measurement windows overlap; ALL attempts are included,
              also failed ones, because a failed run that overlapped a valid
              one would have disturbed it
  conditions  every valid run: marked valid, governor and EPP as planned, power
              cable in at start and end, energy (package and DRAM) present,
              work complete (CPU and memory operations, I/O bytes), positive duration
Reported as numbers, not as errors (INFO): run order, idle power at start and
end (drift), temperature at the start of the runs, settle time-outs,
throttling, CPU time of other programs, perf against RAPL file energy,
session length.

Writes into the session folder:
  results_all.csv    one row per planned run with a valid attempt: schedule
                     columns (order, phase, round, position, part_order) +
                     the result columns of benchlib.RESULT_COLUMNS
  parts_all.csv      one row per job of those runs (case_id + benchlib.PARTS_COLUMNS)
  attempts_all.csv   one row per attempt, also failed ones (window, verdict, reasons)
  check.txt          the report; last line RESULT: PASSED or RESULT: FAILED: ...
Exit 0 = passed, 1 = failed. Standard library only.
"""

import csv
import os
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import benchlib  # noqa: E402
import cases  # noqa: E402

SCHEDULE_COLUMNS = ["order", "phase", "round", "position", "part_order"]
ATTEMPT_COLUMNS = ["run_id", "case_id", "attempt", "has_window", "t_start_us", "t_end_us", "duration_s",
                   "verdict", "hard_reasons", "soft_flags", "is_the_valid_attempt"]


def read_rows(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def num(v):
    return benchlib.to_float(v) if v not in (None, "") else None


def verdict_of(path):
    lines = benchlib.read_text(path).splitlines()
    first = lines[0].strip() if lines else ""
    hard = soft = ""
    for line in lines[1:]:
        if line.startswith("hard:"):
            hard = line[5:].strip()
        elif line.startswith("soft:"):
            soft = line[5:].strip()
    return first, hard, soft


def collect(session):
    """-> (schedule rows, valid result rows, part rows, attempt rows)"""
    schedule = cases.read_schedule(os.path.join(session, "schedule.csv"))
    results, parts, attempts = [], [], []
    for s in schedule:
        rid = s["run_id"]
        run_dir = os.path.join(session, "runs", rid)
        valid_att = benchlib.read_text(os.path.join(run_dir, "VALID")).strip()
        try:
            names = sorted((n for n in os.listdir(run_dir) if n[:1] == "a" and n[1:].isdigit()), key=lambda n: int(n[1:]))
        except OSError:
            names = []
        for n in names:
            res = read_rows(os.path.join(run_dir, n, "result.csv"))
            r = res[0] if res else {}
            verdict, hard, soft = verdict_of(os.path.join(run_dir, n, "verdict.txt"))
            attempts.append({"run_id": rid, "case_id": s["case_id"], "attempt": n,
                             "has_window": bool(r.get("t_start_us") and r.get("t_end_us")),
                             "t_start_us": r.get("t_start_us", ""), "t_end_us": r.get("t_end_us", ""),
                             "duration_s": r.get("duration_s", ""), "verdict": verdict or "none",
                             "hard_reasons": hard, "soft_flags": soft, "is_the_valid_attempt": n == valid_att})
        if valid_att:
            res = read_rows(os.path.join(run_dir, valid_att, "result.csv"))
            if res:
                row = {k: s.get(k, "") for k in SCHEDULE_COLUMNS}
                row.update(res[0])
                results.append(row)
            for p in read_rows(os.path.join(run_dir, valid_att, "parts.csv")):
                p["case_id"] = s["case_id"]
                parts.append(p)
    return schedule, results, parts, attempts


def condition_problems(r):
    """What is wrong with a row that claims to be valid (run_case.sh should never let this through)."""
    p = []
    if r.get("valid") != "1":
        p.append("valid=0")
    if r.get("governor_planned") and r.get("governor_start") != r.get("governor_planned"):
        p.append(f"governor {r.get('governor_start') or '?'} instead of {r['governor_planned']}")
    if r.get("epp_planned") and r.get("epp_start") != r.get("epp_planned"):
        p.append(f"EPP {r.get('epp_start') or '?'} instead of {r['epp_planned']}")
    for side in ("ac_start", "ac_end"):
        if r.get(side) not in ("", None, "1"):
            p.append(f"{side}={r[side]}")
    for key in ("pkg_energy_j", "dram_energy_j", "duration_s"):
        v = num(r.get(key))
        if v is None or v <= 0:
            p.append(f"{key} missing or not positive")
    for done, target in (("cpu_ops_done", "cpu_ops_target"), ("vm_ops_done", "vm_ops_target"),
                         ("io_bytes_done", "io_bytes_target")):
        t, d = num(r.get(target)), num(r.get(done))
        if t is not None and (d is None or d < t):
            p.append(f"{done} {r.get(done) or '?'} < {target} {r[target]}")
    return p


def overlaps(attempts):
    """Pairs of measurement windows that overlap (all attempts with a window), and the smallest gap in s."""
    wins = sorted(((int(num(a["t_start_us"])), int(num(a["t_end_us"])), f"{a['run_id']}/{a['attempt']}")
                   for a in attempts if a["has_window"]), key=lambda w: w[0])
    bad, gaps = [], []
    latest_end, latest_name = None, None
    for start, end, name in wins:
        if latest_end is not None:
            if start < latest_end:
                bad.append((latest_name, name, (latest_end - start) / 1e6))
            else:
                gaps.append((start - latest_end) / 1e6)
        if latest_end is None or end > latest_end:
            latest_end, latest_name = end, name
    return bad, (min(gaps) if gaps else None), len(wins)


def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def check(session, step="?"):
    schedule, results, parts, attempts = collect(session)
    lines, fails = [], []

    def add(level, topic, text):
        lines.append(f"{level:<5} {topic:<12} {text}")
        if level == "FAIL":
            fails.append(topic)

    by_id = {r["run_id"]: r for r in results}
    # complete
    missing = [s["run_id"] for s in schedule if s["run_id"] not in by_id]
    phases = {}
    for s in schedule:
        n_ok, n_all = phases.get(s["phase"], (0, 0))
        phases[s["phase"]] = (n_ok + (s["run_id"] in by_id), n_all + 1)
    detail = ", ".join(f"{k.replace('_', ' ')} {v[0]}/{v[1]}" for k, v in phases.items())
    if missing:
        add("FAIL", "complete", f"{len(missing)} of {len(schedule)} planned runs have no valid attempt: "
                                + " ".join(missing[:20]) + (" ..." if len(missing) > 20 else "") + f" ({detail})")
    else:
        add("OK", "complete", f"{len(schedule)} of {len(schedule)} planned runs valid ({detail})")

    # overlap
    bad, min_gap, n_win = overlaps(attempts)
    if bad:
        add("FAIL", "overlap", f"{len(bad)} overlapping window pair(s): "
                               + "; ".join(f"{a} and {b} by {s:.1f} s" for a, b, s in bad[:10]))
    else:
        add("OK", "overlap", f"no two measurement windows overlap ({n_win} windows incl. failed attempts"
                             + (f"; smallest gap {min_gap:.1f} s)" if min_gap is not None else ")"))

    # conditions
    wrong = [(r["run_id"], condition_problems(r)) for r in results]
    wrong = [(rid, p) for rid, p in wrong if p]
    if wrong:
        add("FAIL", "conditions", f"{len(wrong)} valid run(s) break a condition: "
                                  + "; ".join(f"{rid}: {', '.join(p)}" for rid, p in wrong[:10]))
    else:
        add("OK", "conditions", "every valid run: governor/EPP as planned, power cable in, energy present, work complete")

    # order (information)
    started = sorted((r for r in results if num(r.get("t_start_us")) is not None), key=lambda r: num(r["t_start_us"]))
    planned_pos = {s["run_id"]: i for i, s in enumerate(schedule)}
    out_of_order = sum(1 for a, b in zip(started, started[1:]) if planned_pos[b["run_id"]] < planned_pos[a["run_id"]])
    # which pass made a run valid: progress.txt "<time> <run_id> VALID ... pass=<pass> ..."
    later = sum(1 for line in benchlib.read_text(os.path.join(session, "progress.txt")).splitlines()
                if len(line.split()) > 2 and line.split()[2] == "VALID" and "pass=" in line
                and line.split("pass=")[1].split()[0] in ("repair", "probe_repair"))
    add("INFO", "order", f"valid runs started in schedule order except {out_of_order} place(s); "
                         f"{later} run(s) became valid only in the repair pass")

    # idle drift
    def idle_power(phase, key):
        return mean([num(r.get(key)) for r in results if r.get("phase") == phase])
    s_pkg, e_pkg = idle_power("idle_start", "pkg_power_w"), idle_power("idle_end", "pkg_power_w")
    s_dram, e_dram = idle_power("idle_start", "dram_power_w"), idle_power("idle_end", "dram_power_w")
    if s_pkg is not None and e_pkg is not None:
        add("INFO", "idle", f"package at idle: start {s_pkg:.2f} W, end {e_pkg:.2f} W ({(e_pkg - s_pkg) / s_pkg * 100:+.1f} %)"
                            + (f"; DRAM {s_dram:.2f} / {e_dram:.2f} W" if s_dram is not None and e_dram is not None else ""))

    # temperature, settle
    temps = [num(r.get("temp_start_c")) for r in results if num(r.get("temp_start_c")) is not None]
    timeouts = sum(1 for r in results if r.get("settle_timeout") == "1")
    if temps:
        add("INFO", "temperature", f"package at the start of a run: {min(temps):.0f}-{max(temps):.0f} C "
                                   f"(median {statistics.median(temps):.0f}); {timeouts} run(s) started after the settle limit")

    # throttling
    thr = [(num(r.get("thr_pkg_count")) or 0) + (num(r.get("thr_core_count")) or 0) for r in results]
    add("INFO", "throttle", f"{sum(1 for t in thr if t > 0)} valid run(s) with thermal throttling events "
                            f"({int(sum(thr))} events in total)")

    # background
    other = [num(r.get("other_cpu_per_s")) for r in results if num(r.get("other_cpu_per_s")) is not None]
    kept = sum(1 for r in results if "disturbed_kept_last_attempt" in (r.get("soft_flags") or ""))
    if other:
        add("INFO", "background", f"CPU of other programs during the windows: median {statistics.median(other):.3f}, "
                                  f"max {max(other):.3f} CPUs; {kept} run(s) kept although disturbed")

    # perf
    diffs, no_perf = [], 0
    for r in results:
        if r.get("logging") != "1":
            continue
        pe, fe = num(r.get("perf_pkg_energy_j")), num(r.get("pkg_energy_j"))
        if num(r.get("instructions")) is None:
            no_perf += 1
        if pe and fe:
            diffs.append(abs(pe - fe) / fe * 100)
    add("INFO", "perf", (f"perf package energy vs RAPL file: median difference {statistics.median(diffs):.1f} %, "
                         f"max {max(diffs):.1f} % (perf's window is a few ms longer); " if diffs else "")
                        + f"{no_perf} logged run(s) without perf counters")

    # length
    starts = [num(a["t_start_us"]) for a in attempts if a["has_window"]]
    ends = [num(a["t_end_us"]) for a in attempts if a["has_window"]]
    if starts and ends:
        add("INFO", "duration", f"first window to last: {(max(ends) - min(starts)) / 3.6e9:.1f} h; "
                                f"{len(attempts)} attempts for {len(schedule)} planned runs")

    # tables
    benchlib.write_csv(os.path.join(session, "results_all.csv"), SCHEDULE_COLUMNS + benchlib.RESULT_COLUMNS, results)
    benchlib.write_csv(os.path.join(session, "parts_all.csv"), ["case_id"] + benchlib.PARTS_COLUMNS, parts)
    benchlib.write_csv(os.path.join(session, "attempts_all.csv"), ATTEMPT_COLUMNS, attempts)

    verdict = "PASSED" if not fails else "FAILED: " + ", ".join(dict.fromkeys(fails))
    text = (f"CHECK {step} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n" + "\n".join(lines)
            + f"\nRESULT: {verdict}\n")
    with open(os.path.join(session, "check.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text, not fails


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text, passed = check(argv[1], argv[2] if len(argv) == 3 else "?")
    except (OSError, ValueError, KeyError) as e:
        print(f"check_session: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
