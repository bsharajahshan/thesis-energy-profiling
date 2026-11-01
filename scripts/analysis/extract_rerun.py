#!/usr/bin/env python3

"""
extract_rerun.py - turn the archives of the fixed-work re-run into clean tables

Input: the archives the laptop wrote. The first one is the session to turn
into tables: thesis_main.tar.gz (optionally followed by the pilot and
memory-check archives, unpacked only), or thesis_govcheck.tar.gz for the
governor check. Output, in <out_dir>:
  raw/<name>/            each archive, unpacked as it was
  tables/cases.csv       the cases of the session's schedule (main: the 52 cases + idle;
                         governor check: 9 cases + idle): block, RQ, parts, sizes
  tables/runs.csv        one row per valid run of the session (main: 270 rows):
                         case description + every column of results_all.csv
  tables/parts.csv       the parts (sub-tasks) of the valid attempts
  tables/attempts.csv    every attempt, also the retried ones
  tables/samples_1s.csv  sampler, about one line per second: package and DRAM power, temperature
  tables/perf_1s.csv     perf stat, one line per second and event (runs with logging only)
  MANIFEST.txt           inputs (sha256), outputs, row counts

Nothing is computed here beyond unit conversion (energy counter -> watts per
sample) and joining the case description; the analysis does the rest.
Standard library only, so it runs with any Python 3.10+.

Usage (from the repo root):
  python3 -I scripts/analysis/extract_rerun.py <out_dir> <main.tar.gz> [<other.tar.gz> ...]
  python3 -I scripts/analysis/extract_rerun.py results/rerun_2026-10-31/govcheck thesis_govcheck.tar.gz
"""

import csv
import hashlib
import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bench"))
import cases  # noqa: E402


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def unpack(archive, dest):
    """Unpack into a fresh folder; only plain files and folders inside it."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (m.isfile() or m.isdir()) or not target.startswith(os.path.realpath(dest) + os.sep):
                raise SystemExit(f"refusing {m.name!r} in {archive}")
        tar.extractall(dest)


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---- case table -------------------------------------------------------------

def describe(case):
    parts = case["parts"]
    kinds = [p["kind"] for p in parts]
    cpu = [p for p in parts if p["kind"] == "cpu"]
    vm = [p for p in parts if p["kind"] == "vm"]
    io = [p for p in parts if p["kind"] == "io"]
    fixed_time = any(p.get("fixed") is True for p in parts) or kinds == ["idle"]
    return {
        "case_id": case["case_id"],
        "block": case["block"],
        "rq": case["rq"],
        "mode": case["mode"],
        "parts": "+".join(kinds),
        "n_parts": len(parts),
        "cpu_threads_per_part": cpu[0]["threads"] if cpu else "",
        # threads busy at the same time: all CPU parts in a parallel case, one part in a sequential one
        "cpu_threads_total": "" if not cpu else
                             sum(p["threads"] for p in cpu) if case["mode"] == "parallel" else cpu[0]["threads"],
        "cpu_load_pct": cpu[0]["load"] if cpu else "",
        "cpu_work_share_per_part": str(cpu[0]["frac"]) if cpu else "",
        "vm_gb_per_part": vm[0]["gb"] if vm else "",
        "vm_work_share_per_part": str(vm[0]["frac"]) if vm else "",
        "io_pattern": io[0]["pattern"] if io else "",
        "pin": ";".join(str(p["pin"]) for p in parts if p.get("pin")),
        "size": "fixed time" if fixed_time else "fixed work",
        "governor": case["governor"],
        "logging": int(case["logging"]),
    }


CASE_COLUMNS = list(describe(cases.IDLE_CASE))


# ---- runs, parts, attempts ----------------------------------------------------

def read_csv_dedup(path):
    """results_all.csv starts with the schedule columns; drop repeated names (they hold the same values)."""
    with open(path, newline="") as f:
        r = csv.reader(f)
        header = next(r)
        keep, seen = [], set()
        for i, name in enumerate(header):
            if name not in seen:
                seen.add(name)
                keep.append(i)
        rows = [{header[i]: row[i] for i in keep} for row in r]
    return [header[i] for i in keep], rows


def rep_of(row):
    if row["phase"] == "round":
        return row["round"]
    return row["run_id"].rsplit("_", 1)[-1]   # idle_start_3 -> 3


def write_csv(path, columns, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# ---- time series --------------------------------------------------------------

def key_values(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            f = line.split()
            if len(f) >= 2:
                out.setdefault(f[0], f[1:])
    return out


def samples(run_dir, run_id):
    """sampler.txt -> power per interval; t_s relative to the start of the measurement window.
    The *_nolog control runs have no sampler (that is the point of them)."""
    path = os.path.join(run_dir, "sampler.txt")
    if not os.path.exists(path):
        return []
    lines = [l.split() for l in open(path) if l.strip()]
    win = key_values(os.path.join(run_dir, "window.txt"))
    snap = key_values(os.path.join(run_dir, "snap_before.txt"))
    t0, t1 = float(win["win_start"][0]), float(win["win_end"][0])
    pkg_max = to_float(snap.get("pkg_max_uj", [None])[0])
    dram_max = to_float(snap.get("dram_max_uj", [None])[0])

    def watts(a, b, dt, top):
        a, b = to_float(a), to_float(b)
        if a is None or b is None or dt <= 0:
            return ""
        d = b - a
        if d < 0 and top:
            d += top
        return round(d / 1e6 / dt, 4)

    out = []
    for prev, cur in zip(lines, lines[1:]):
        if len(prev) < 4 or len(cur) < 4:
            continue
        t_prev, t_cur = float(prev[0]), float(cur[0])
        dt = t_cur - t_prev
        temp = to_float(cur[3])
        out.append({"run_id": run_id, "t_s": round(t_cur - t0, 3),
                    "in_window": int(t_prev >= t0 and t_cur <= t1),
                    "pkg_w": watts(prev[1], cur[1], dt, pkg_max),
                    "dram_w": watts(prev[2], cur[2], dt, dram_max),
                    "temp_c": temp / 1000 if temp is not None else ""})
    return out


def perf_lines(run_dir, run_id):
    path = os.path.join(run_dir, "perf.csv")
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        if not line.strip() or line.startswith("#") or line.startswith("summary,"):
            continue
        f = line.rstrip("\n").split(",")
        if len(f) < 4:
            continue
        out.append({"run_id": run_id, "t_s": f[0], "event": f[3], "value": f[1], "unit": f[2]})
    return out


# ---- main -----------------------------------------------------------------------

def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    out, archives = argv[1], argv[2:]
    raw, tables = os.path.join(out, "raw"), os.path.join(out, "tables")
    os.makedirs(tables, exist_ok=True)
    manifest = [f"extract_rerun.py {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "", "inputs:"]
    for a in archives:
        name = os.path.basename(a).replace(".tar.gz", "").replace("thesis_", "")
        unpack(a, os.path.join(raw, name))
        manifest.append(f"  {os.path.basename(a)}  sha256 {sha256(a)}  -> raw/{name}/")

    step = os.path.basename(archives[0]).replace(".tar.gz", "").replace("thesis_", "")
    session = os.path.join(raw, step, step)     # thesis_<step>.tar.gz holds the folder <step>/
    counts = {}

    planned = {r["case_id"] for r in cases.read_schedule(os.path.join(session, "schedule.csv"))}
    case_rows = [describe(c) for c in cases.CASES + cases.GOVCHECK_CASES + [cases.IDLE_CASE] if c["case_id"] in planned]
    by_case = {c["case_id"]: c for c in case_rows}
    counts["cases.csv"] = write_csv(os.path.join(tables, "cases.csv"), CASE_COLUMNS, case_rows)

    res_cols, res = read_csv_dedup(os.path.join(session, "results_all.csv"))
    valid = [r for r in res if r["valid"] == "1"]
    # columns that results_all.csv already has (block, rq, mode, n_parts, logging) come from there
    run_cols = ["run_id", "case_id", "rep"] + [c for c in CASE_COLUMNS if c not in res_cols] + \
               [c for c in res_cols if c not in ("run_id", "case_id")]
    rows = []
    for r in valid:
        row = dict(by_case[r["case_id"]])
        row.update(r)
        row["rep"] = rep_of(r)
        rows.append(row)
    counts["runs.csv"] = write_csv(os.path.join(tables, "runs.csv"), run_cols, rows)

    valid_att = {(r["run_id"], r["attempt"]) for r in valid}
    with open(os.path.join(session, "parts_all.csv"), newline="") as f:
        pr = csv.DictReader(f)
        part_rows = [p for p in pr if (p["run_id"], p["attempt"]) in valid_att]
        counts["parts.csv"] = write_csv(os.path.join(tables, "parts.csv"), pr.fieldnames, part_rows)
    shutil.copyfile(os.path.join(session, "attempts_all.csv"), os.path.join(tables, "attempts.csv"))
    counts["attempts.csv"] = sum(1 for _ in open(os.path.join(tables, "attempts.csv"))) - 1

    sample_rows, perf_rows = [], []
    for r in valid:
        run_dir = os.path.join(session, "runs", r["run_id"], "a" + r["attempt"])
        sample_rows += samples(run_dir, r["run_id"])
        perf_rows += perf_lines(run_dir, r["run_id"])
    counts["samples_1s.csv"] = write_csv(os.path.join(tables, "samples_1s.csv"),
                                         ["run_id", "t_s", "in_window", "pkg_w", "dram_w", "temp_c"], sample_rows)
    counts["perf_1s.csv"] = write_csv(os.path.join(tables, "perf_1s.csv"),
                                      ["run_id", "t_s", "event", "value", "unit"], perf_rows)

    manifest += ["", "tables (rows without header):"] + [f"  {k:<16} {v}" for k, v in counts.items()]
    with open(os.path.join(out, "MANIFEST.txt"), "w") as f:
        f.write("\n".join(manifest) + "\n")
    print("\n".join(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
