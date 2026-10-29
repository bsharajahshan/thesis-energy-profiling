#!/usr/bin/env python3

"""
pilot_report.py - what the pilot found, in one text file (pilot_report.txt)

session.sh runs it at the end of the pilot, after check_session.py (it reads
results_all.csv and attempts_all.csv). Sections:
  start check      the RESULT line and every FAIL/WARN line of preflight.txt
  calibration      calibration_report.txt
  repeatability    the main jobs repeated at their calibrated size: spread of time,
                   energy, instructions and DRAM traffic; work done vs target;
                   instructions per operation (is `all` the same work every time?)
  memory sizes     DRAM traffic of the memory jobs per buffer size vs the 8 GB job
  rehearsal        each case once: window and wall time; estimated main session length
  recovery         the injected failed attempt and the killed Nextflow call
  perf             where the totals came from (summary lines or summed intervals)
  points to check  everything above that looks wrong
Usage: pilot_report.py <session>
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

SPREAD_TIME = 5.0          # % above which the spread of repeated jobs is a point to check
SPREAD_TRAFFIC = 10.0
SPREAD_INSTR = 2.0


def num(v):
    return benchlib.to_float(v) if v not in (None, "") else None


def read_rows(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def cv(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2 or statistics.mean(vals) == 0:
        return None
    return statistics.stdev(vals) / statistics.mean(vals) * 100


def traffic(r):
    a, b = num(r.get("dram_read_gib")), num(r.get("dram_write_gib"))
    return a + b if a is not None and b is not None else None


def work_target(r):
    for done, target in (("cpu_ops_done", "cpu_ops_target"), ("vm_ops_done", "vm_ops_target"),
                         ("io_bytes_done", "io_bytes_target")):
        if num(r.get(target)) is not None:
            return num(r.get(done)), num(r.get(target))
    return None, None


def fmt_cv(x):
    return f"{x:.1f} %" if x is not None else "-"


def report(session):
    results = read_rows(os.path.join(session, "results_all.csv"))
    attempts = read_rows(os.path.join(session, "attempts_all.csv"))
    out, points = [], []
    out.append(f"PILOT REPORT {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    # start check
    out.append("\n== start check (preflight.txt)")
    pre = benchlib.read_text(os.path.join(session, "preflight.txt")).splitlines()
    out += [f"  {l}" for l in pre if l.startswith(("FAIL", "WARN", "RESULT"))] or ["  (no preflight.txt)"]
    points += [f"start check: {l[6:].strip()}" for l in pre if l.startswith("WARN")]

    # calibration
    out.append("\n== calibration (calibration_report.txt)")
    cal_text = benchlib.read_text(os.path.join(session, "calibration_report.txt"))
    out += [f"  {l}" for l in cal_text.splitlines()] or ["  (calibration did not run)"]
    if "nothing written" in cal_text or not cal_text:
        points.append("calibration: nothing written; the pilot used the placeholder sizes")
    points += [f"calibration: {l.strip()}" for l in cal_text.splitlines() if "kept at" in l]

    # repeatability
    out.append("\n== repeatability (phase 'repeat': each main job several times at its calibrated size)")
    rep = {}
    for r in results:
        if r.get("phase") == "repeat":
            rep.setdefault(r["case_id"], []).append(r)
    if not rep:
        out.append("  no valid repeat runs")
        points.append("repeatability: no valid repeat runs")
    for cid, rows in rep.items():
        d = [num(r.get("duration_s")) for r in rows]
        e = [num(r.get("pkg_energy_j")) for r in rows]
        ins = [num(r.get("instructions")) for r in rows]
        tr = [traffic(r) for r in rows]
        done_target = [work_target(r) for r in rows]
        per_op = [i / w[0] for i, w in zip(ins, done_target) if i and w[0]]
        out.append(f"  {cid}: n={len(rows)}, time {statistics.mean([x for x in d if x]):.1f} s (spread {fmt_cv(cv(d))}), "
                   f"package energy spread {fmt_cv(cv(e))}, instructions spread {fmt_cv(cv(ins))}, "
                   f"DRAM traffic {statistics.mean([x for x in tr if x is not None]) if any(x is not None for x in tr) else float('nan'):.1f} GiB "
                   f"(spread {fmt_cv(cv(tr))}), instructions per operation spread {fmt_cv(cv(per_op))}")
        for name, value, limit in (("time", cv(d), SPREAD_TIME), ("DRAM traffic", cv(tr), SPREAD_TRAFFIC),
                                   ("instructions", cv(ins), SPREAD_INSTR)):
            if value is not None and value > limit:
                points.append(f"repeatability: {cid} {name} spread {value:.1f} % (above {limit:g} %)")
        if any(w[0] is not None and w[1] is not None and w[0] < w[1] for w in done_target):
            points.append(f"repeatability: {cid} did not complete its work in every repeat")

    # memory sizes
    out.append("\n== memory sizes (DRAM traffic of a memory job, should be the same for every size)")
    mem = {}
    for r in results:
        if r.get("case_id") in ("mem_1g", "mem_2g", "mem_4g", "mem_8g") and traffic(r) is not None:
            mem.setdefault(r["case_id"], []).append(traffic(r))
    if "mem_8g" in mem:
        ref = statistics.mean(mem["mem_8g"])
        for cid in ("mem_1g", "mem_2g", "mem_4g", "mem_8g"):
            if cid in mem:
                m = statistics.mean(mem[cid])
                out.append(f"  {cid}: {m:.1f} GiB ({(m - ref) / ref * 100:+.1f} % vs 8 GB, n={len(mem[cid])})")
                if abs(m - ref) / ref > 0.10:
                    points.append(f"memory sizes: {cid} moves {(m - ref) / ref * 100:+.1f} % data compared to 8 GB")
    else:
        out.append("  no valid 8 GB memory job")

    # rehearsal
    out.append("\n== rehearsal (one round of every case)")
    wall = {}
    for line in benchlib.read_text(os.path.join(session, "progress.txt")).splitlines():
        f = line.split()
        if len(f) >= 3:
            kv = dict(x.split("=", 1) for x in f[3:] if "=" in x)
            wall.setdefault(f[1], 0.0)
            wall[f[1]] += float(kv.get("seconds", 0) or 0)
    round_rows = [r for r in results if r.get("phase") == "round"]
    idle_rows = [r for r in results if r.get("phase") in ("idle_start", "idle_end")]
    if round_rows:
        longest = sorted(round_rows, key=lambda r: -(num(r.get("duration_s")) or 0))[:8]
        out.append("  longest windows: " + ", ".join(f"{r['case_id']} {num(r.get('duration_s')):.0f} s" for r in longest))
        round_wall = sum(wall.get(r["run_id"], 0.0) for r in round_rows)
        idle_wall = statistics.mean([wall.get(r["run_id"], 0.0) for r in idle_rows]) if idle_rows else 120.0
        cal = benchlib.read_env(os.path.join(session, "calibration.env"))
        brk = float(cal.get("BREAK_S") or 600)
        n_cases = len(cases.CASES)
        missing = n_cases - len(round_rows)
        est = 5 * round_wall * (n_cases / max(1, len(round_rows))) + 10 * idle_wall + 4 * brk
        out.append(f"  {len(round_rows)} of {n_cases} cases valid; one round took {round_wall / 3600:.2f} h of wall time "
                   f"(runs, waits, Nextflow)")
        out.append(f"  estimated main session (5 rounds, 10 idle runs, 4 breaks, repair pass not included): {est / 3600:.1f} h")
        if missing:
            points.append(f"rehearsal: {missing} case(s) without a valid run")
        settle = [num(r.get("settle_s")) for r in round_rows if num(r.get("settle_s")) is not None]
        if settle:
            out.append(f"  wait before a run (settle): median {statistics.median(settle):.0f} s, max {max(settle):.0f} s")
    else:
        out.append("  no valid rehearsal runs")
        points.append("rehearsal: no valid runs")

    # recovery
    out.append("\n== recovery tests")
    rt = benchlib.read_env(os.path.join(session, "recovery_test.txt"))
    by_run = {}
    for a in attempts:
        by_run.setdefault(a["run_id"], []).append(a)
    valid_ids = {r["run_id"] for r in results}
    fail_id, kill_id = rt.get("fail"), rt.get("kill")
    if fail_id:
        att = by_run.get(fail_id, [])
        ok = (len(att) >= 2 and "injected_failure" in (att[0].get("hard_reasons") or "") and fail_id in valid_ids)
        out.append(f"  injected failed attempt ({fail_id}): " + ("PASSED, retried and then valid" if ok else
                   f"NOT PASSED ({len(att)} attempt(s), valid: {fail_id in valid_ids})"))
        if not ok:
            points.append(f"recovery: the injected failure of {fail_id} was not handled as expected")
    if kill_id:
        killed = "killed_nextflow" in benchlib.read_text(os.path.join(session, "recovery_test.txt"))
        att = by_run.get(kill_id, [])
        ok = killed and kill_id in valid_ids and len(att) >= 2
        out.append(f"  killed Nextflow call ({kill_id}): " + ("PASSED, the session went on and the run became valid later"
                   if ok else f"NOT PASSED (killed: {killed}, {len(att)} attempt(s), valid: {kill_id in valid_ids})"))
        if not ok:
            points.append(f"recovery: the killed Nextflow call of {kill_id} was not handled as expected")
    if not fail_id and not kill_id:
        out.append("  not set up")

    # perf
    out.append("\n== perf")
    src = {}
    for r in results:
        if r.get("logging") == "1":
            src[r.get("perf_source") or "missing"] = src.get(r.get("perf_source") or "missing", 0) + 1
    out.append("  totals from: " + ", ".join(f"{k} {v}" for k, v in sorted(src.items())))
    if src.get("missing"):
        points.append(f"perf: {src['missing']} logged run(s) without perf counters")
    flags = {}
    for r in results:
        for f in (r.get("soft_flags") or "").split(";"):
            if f:
                flags[f.split(":")[0]] = flags.get(f.split(":")[0], 0) + 1
    if flags:
        out.append("  soft flags on valid runs: " + ", ".join(f"{k} {v}" for k, v in sorted(flags.items())))

    out.append("\n== points to check before the main session")
    out += [f"  - {p}" for p in points] or ["  none"]
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text = report(argv[1])
    except (OSError, ValueError, KeyError, statistics.StatisticsError) as e:
        text = f"PILOT REPORT could not be written: {type(e).__name__}: {e}\n"
    with open(os.path.join(argv[1], "pilot_report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
