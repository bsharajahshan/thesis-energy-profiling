#!/usr/bin/env python3

"""
cases.py - every case of the fixed-work session, and the run specs built from them

A case is one kind of run, e.g. "the CPU job with 4 threads" or "cpu_ram in
parallel". The schedule (make_schedule.py) says in which order the cases run;
this module turns one schedule row into the run spec that run_case.sh
measures, using the job sizes in calibration.env and the CPU topology of the
laptop (for the two pinned cases).

Jobs (sizes in calibration.env, decided by the pilot):
  CPU job     stress-ng --cpu-method all, CPU_OPS bogo-ops in total
  memory job  stress-ng --vm 1 --vm-method all, VM_OPS_<size>G bogo-ops; every
              size moves the same amount of DRAM data
  I/O job     fio, IO_BYTES written in 4 KiB blocks, fsync at the end

Shared jobs: a job that appears in several research questions is measured
once. cpu_t8 is RQ1 "8 threads", RQ3 CPU N=1 (one task is the whole job, so
parallel and sequential are the same run) and the isolated CPU job of RQ2;
mem_8g likewise for memory; io_seq is the isolated I/O job of RQ2. The `rq`
label of these cases lists all research questions that use them.

Governor check (run.sh govcheck, after the main session): three cases of the
main session, each also with governor `performance` (gov_*) and with the
default governor but EPP `performance` (epp_*). GOVCHECK_CASES holds only the
new variants; the default runs are the cases themselves.

Usage on the laptop (called by the session driver):
  cases.py specs <schedule.csv> <calibration.env> <session.env> <out_dir>
      writes <out_dir>/<run_id>.json for every schedule row (needs
      CPU_SIBLINGS in session.env, e.g. "0,4;1,5;2,6;3,7")
  cases.py list
      prints the case table
Standard library only (Python 3.10+).
"""

import csv
import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import benchlib  # noqa: E402

CAL_INT_KEYS = ["CPU_OPS", "VM_OPS_1G", "VM_OPS_2G", "VM_OPS_4G", "VM_OPS_8G", "IO_BYTES"]
CAL_NUM_KEYS = ["MECH_FIXED_S", "IDLE_S", "CPU_S_T1", "CPU_S_T2", "CPU_S_T4", "CPU_S_T8",
                "CPU_S_T8_LOAD50", "CPU_S_T8_LOAD25", "VM_S", "IO_S_SEQ", "IO_S_RAND",
                "SETTLE_EST_S", "NXF_START_S", "BREAK_S"]
CAL_TEXT_KEYS = ["GOVERNOR_MAIN", "EPP_MAIN", "GOVERNOR_CONTROL"]


# ---- parts (symbolic: sizes and pins are filled in later) ----------------------

# fixed: False = fixed work (ops from the calibration), True = fixed time MECH_FIXED_S,
# or the name of another calibration key with the seconds (pilot probes).
# ops_key: a calibration key with an absolute number of operations (pilot probe).
def cpu(threads, frac=1, load=100, fixed=False, pin=None, ops_key=None):
    return {"kind": "cpu", "threads": threads, "frac": Fraction(frac), "load": load, "fixed": fixed, "pin": pin,
            "ops_key": ops_key}


# method / keep: None = VM_METHOD / VM_KEEP of the calibration (memory-check probes set them)
def vm(gb, frac=1, fixed=False, pin=None, method=None, keep=None):
    return {"kind": "vm", "gb": gb, "frac": Fraction(frac), "fixed": fixed, "pin": pin, "method": method, "keep": keep}


def io(pattern, bytes_key="IO_BYTES"):
    return {"kind": "io", "pattern": pattern, "bytes_key": bytes_key}


def idle(seconds_key="IDLE_S"):
    return {"kind": "idle", "seconds_key": seconds_key}


# Sizes of the pilot probes; calibration.env may set them (the tests make them tiny).
PROBE_DEFAULTS = {"PROBE_S": "30", "PROBE_IO_BYTES": str(4 * 1024 ** 3), "PROBE_COOLDOWN_S": "300",
                  "PROBE_SEMANTICS_OPS": "20000"}


def calv(cal, key):
    return cal.get(key) or PROBE_DEFAULTS.get(key) or cal[key]


# Expected seconds of a case, from calibration.env (watchdog and time estimate only).
def secs(key):
    return lambda cal: float(cal[key]) if key in cal else float(PROBE_DEFAULTS[key])


def together(*keys):
    """Jobs running at the same time: rough guess, the longest job plus 80 % of the others
    (they slow each other down; in cpu_ram the memory job ran at 14 % of its speed on 2026-10-15)."""
    def f(cal):
        t = [float(cal[k]) for k in keys]
        return max(t) + 0.8 * (sum(t) - max(t))
    return f


def one_after_another(*keys):
    return lambda cal: sum(float(cal[k]) for k in keys)


CASES = []


# epp: None = EPP_MAIN of the calibration for main-governor runs, untouched for control runs
def add(case_id, block, rq, mode, parts, expected, governor="main", logging=True, epp=None):
    CASES.append({"case_id": case_id, "block": block, "rq": rq, "mode": mode, "parts": parts,
                  "expected": expected, "governor": governor, "logging": logging, "epp": epp})


# RQ1: isolated jobs at four load levels; I/O sequential and random.
for t in (1, 2, 4, 8):
    add(f"cpu_t{t}", "rq1", "rq1+rq2+rq3" if t == 8 else "rq1", "parallel", [cpu(t)], secs(f"CPU_S_T{t}"))
for gb in (1, 2, 4, 8):
    add(f"mem_{gb}g", "rq1", "rq1+rq2+rq3" if gb == 8 else "rq1", "parallel", [vm(gb)], secs("VM_S"))
add("io_seq", "rq1", "rq1+rq2", "parallel", [io("write")], secs("IO_S_SEQ"))
add("io_rand", "rq1", "rq1", "parallel", [io("randwrite")], secs("IO_S_RAND"))

# RQ2: the max-level jobs of RQ1 combined, at the same time and one after another.
RQ2_JOBS = {"cpu": (cpu(8), "CPU_S_T8"), "ram": (vm(8), "VM_S"), "io": (io("write"), "IO_S_SEQ")}
for combo in ("cpu_ram", "cpu_io", "ram_io", "cpu_ram_io"):
    names = combo.split("_")
    parts = [RQ2_JOBS[n][0] for n in names]
    keys = [RQ2_JOBS[n][1] for n in names]
    add(f"rq2_{combo}_par", "rq2", "rq2", "parallel", parts, together(*keys))
    add(f"rq2_{combo}_seq", "rq2", "rq2", "sequential", parts, one_after_another(*keys))

# RQ3: the same total work split into N sub-tasks (8/N threads or 8/N GB each,
# 1/N of the work each). N=1 is cpu_t8 / mem_8g. CPU also at 50 % and 25 % load
# (parallel only; N=1 included, it is not the same job as cpu_t8).
for n in (2, 4, 8):
    add(f"rq3_cpu_par_n{n}", "rq3", "rq3", "parallel", [cpu(8 // n, Fraction(1, n))] * n, secs("CPU_S_T8"))
    add(f"rq3_cpu_seq_n{n}", "rq3", "rq3", "sequential", [cpu(8 // n, Fraction(1, n))] * n, secs(f"CPU_S_T{8 // n}"))
    add(f"rq3_ram_par_n{n}", "rq3", "rq3", "parallel", [vm(8 // n, Fraction(1, n))] * n, secs("VM_S"))
    add(f"rq3_ram_seq_n{n}", "rq3", "rq3", "sequential", [vm(8 // n, Fraction(1, n))] * n, secs("VM_S"))
for load in (50, 25):
    for n in (1, 2, 4, 8):
        add(f"rq3_cpu_load{load}_n{n}", "rq3", "rq3", "parallel",
            [cpu(8 // n, Fraction(1, n), load=load)] * n, secs(f"CPU_S_T8_LOAD{load}"))

# Mechanism block (fixed time, operations per second): why does the memory job
# slow down next to the CPU job? Unpinned with 8 / 7 / 4 CPU threads, and two
# pinned cases (Philipp: pinning only as this extra block): memory worker on a
# core of its own (its second hardware thread idle), or sharing its core with
# a busy CPU thread.
add("mech_vm_alone", "mech", "mech", "parallel", [vm(8, fixed=True)], secs("MECH_FIXED_S"))
for t in (8, 7, 4):
    add(f"mech_cpu{t}_vm", "mech", "mech", "parallel", [cpu(t, fixed=True), vm(8, fixed=True)], secs("MECH_FIXED_S"))
add("mech_pin_own_core", "mech", "mech", "parallel",
    [cpu(6, fixed=True, pin="@other_cores"), vm(8, fixed=True, pin="@mem_cpu")], secs("MECH_FIXED_S"))
add("mech_pin_shared_core", "mech", "mech", "parallel",
    [cpu(7, fixed=True, pin="@all_but_mem_cpu"), vm(8, fixed=True, pin="@mem_cpu")], secs("MECH_FIXED_S"))

# Controls: governor 'performance' instead of the default 'powersave' (RQ1 CPU
# curve), and the cost of the measurement itself (no perf, no sampler).
for t in (1, 2, 4, 8):
    add(f"gov_cpu_t{t}", "gov", "control", "parallel", [cpu(t)], secs(f"CPU_S_T{t}"), governor="control")
add("cost_cpu_t8_nolog", "cost", "control", "parallel", [cpu(8)], secs("CPU_S_T8"), logging=False)
add("cost_mem_8g_nolog", "cost", "control", "parallel", [vm(8)], secs("VM_S"), logging=False)
add("cost_cpu_ram_par_nolog", "cost", "control", "parallel", [cpu(8), vm(8)], together("CPU_S_T8", "VM_S"),
    logging=False)
add("cost_idle_nolog", "cost", "control", "parallel", [idle()], secs("IDLE_S"), logging=False)

# Governor check (its own session, after the main session): do Ubuntu's default
# power settings cause the low clock of the 50 % load runs (phases at about
# 1.2 GHz) and of CPU + memory in parallel (about 2.4 GHz, below the power
# limit)? The same jobs with governor 'performance' (the kernel then also sets
# EPP 'performance'), and with the default governor but EPP 'performance' (what
# Ubuntu's 'Performance' power mode sets). The default runs are the cases themselves.
GOVCHECK_BASE = ["rq3_cpu_load50_n1", "rq3_cpu_load50_n4", "rq2_cpu_ram_par"]
GOVCHECK_CASES = []
for _cid in GOVCHECK_BASE:
    _c = next(c for c in CASES if c["case_id"] == _cid)
    GOVCHECK_CASES.append(dict(_c, case_id=f"gov_{_cid}", block="govcheck", rq="control", governor="control"))
    GOVCHECK_CASES.append(dict(_c, case_id=f"epp_{_cid}", block="govcheck", rq="control", epp="performance"))

# Idle runs before the first and after the last round (not inside the rounds).
IDLE_CASE = {"case_id": "idle", "block": "idle", "rq": "all", "mode": "parallel", "parts": [idle()],
             "expected": secs("IDLE_S"), "governor": "main", "logging": True}

# Pilot probes (phase 'probe' of the pilot schedule only): short fixed-time runs
# that measure the speed of every job type, from which calibrate.py computes
# the job sizes. Order in the pilot: idle first (background, idle power and
# DRAM traffic), the cooling curve last.
PILOT_CASES = []


def add_probe(case_id, mode, parts, expected):
    PILOT_CASES.append({"case_id": case_id, "block": "probe", "rq": "pilot", "mode": mode, "parts": parts,
                        "expected": expected, "governor": "main", "logging": True})


add_probe("probe_idle", "parallel", [idle()], secs("IDLE_S"))
# how stress-ng counts --cpu-ops: 4 workers, PROBE_SEMANTICS_OPS asked; about 4x more done = per worker
add_probe("probe_ops_semantics", "parallel", [cpu(4, ops_key="PROBE_SEMANTICS_OPS")], secs("PROBE_S"))
for t in (1, 2, 4, 8):
    add_probe(f"probe_cpu_t{t}", "parallel", [cpu(t, fixed="PROBE_S")], secs("PROBE_S"))
for load in (50, 25):
    add_probe(f"probe_cpu_load{load}", "parallel", [cpu(8, load=load, fixed="PROBE_S")], secs("PROBE_S"))
for gb in (1, 2, 4, 8):
    add_probe(f"probe_mem_{gb}g", "parallel", [vm(gb, fixed="PROBE_S")], secs("PROBE_S"))
add_probe("probe_io_seq", "parallel", [io("write", bytes_key="PROBE_IO_BYTES")], lambda cal: 2 * float(cal["IO_S_SEQ"]))
add_probe("probe_io_rand", "parallel", [io("randwrite", bytes_key="PROBE_IO_BYTES")], lambda cal: 2 * float(cal["IO_S_RAND"]))
# cooling curve: 60 s with 8 CPU threads, then idle; the sampler logs the temperature every second
add_probe("probe_cooldown", "sequential", [cpu(8, fixed="MECH_FIXED_S"), idle(seconds_key="PROBE_COOLDOWN_S")],
          lambda cal: float(cal["MECH_FIXED_S"]) + float(calv(cal, "PROBE_COOLDOWN_S")))

# Memory check (run.sh memcheck): every candidate vm method of MEMCHECK_METHODS at
# every buffer size, PROBE_S fixed time with --vm-keep; calibrate.py picks the
# method whose DRAM data per operation per GB is steadiest across sizes.
MEMCHECK_DEFAULT_METHODS = ("all", "write64", "read64", "wrrd128nt")
MEMCHECK_CASES = []
for _m in MEMCHECK_DEFAULT_METHODS:
    for _gb in (1, 2, 4, 8):
        MEMCHECK_CASES.append({"case_id": f"probe_vmm_{_m}_{_gb}g", "block": "probe", "rq": "memcheck",
                               "mode": "parallel", "parts": [vm(_gb, fixed="PROBE_S", method=_m, keep=True)],
                               "expected": secs("PROBE_S"), "governor": "main", "logging": True})

CASE_BY_ID = {c["case_id"]: c for c in CASES + [IDLE_CASE] + PILOT_CASES + MEMCHECK_CASES + GOVCHECK_CASES}


def mixed_sequential(case):
    """Sequential case with different jobs (RQ2): the job order is shuffled per run."""
    return case["mode"] == "sequential" and len({p["kind"] for p in case["parts"]}) > 1


# ---- calibration and topology -----------------------------------------------------

def load_calibration(path):
    cal = benchlib.read_env(path)
    problems = []
    for k in CAL_INT_KEYS:
        v = benchlib.to_int(cal.get(k))
        if v is None or v < 1:
            problems.append(f"{k} must be a whole number >= 1 (got {cal.get(k)!r})")
    for k in CAL_NUM_KEYS:
        v = benchlib.to_float(cal.get(k))
        if v is None or v < 0:
            problems.append(f"{k} must be a number >= 0 (got {cal.get(k)!r})")
    for k in CAL_TEXT_KEYS:
        if not cal.get(k):
            problems.append(f"{k} is missing")
    if not problems:
        if int(cal["CPU_OPS"]) % 8:
            problems.append("CPU_OPS must be a multiple of 8 (RQ3 splits it into 2, 4, 8 equal parts)")
        if int(cal["IO_BYTES"]) % 4096:
            problems.append("IO_BYTES must be a multiple of 4096 (4 KiB blocks)")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return cal


def parse_cpu_list(text):
    """'0,4' / '0-1' / '0-2,4' -> [0, 4] / [0, 1] / [0, 1, 2, 4]"""
    cpus = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            a, b = item.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(item))
    return sorted(set(cpus))


def pin_sets(siblings):
    """CPU_SIBLINGS ("0,4;1,5;2,6;3,7": logical CPUs of each physical core) -> pin tokens.

    The memory worker gets the first logical CPU of the last core. Tokens that
    need a second hardware thread on that core are missing without SMT."""
    groups = sorted(parse_cpu_list(g) for g in siblings.split(";") if g.strip())
    if len(groups) < 2:
        raise ValueError(f"CPU_SIBLINGS needs at least two cores: {siblings!r}")
    mem_core = groups[-1]
    others = sorted(c for g in groups[:-1] for c in g)
    pins = {"@mem_cpu": str(mem_core[0]), "@other_cores": ",".join(map(str, others))}
    if len(mem_core) > 1:
        pins["@all_but_mem_cpu"] = ",".join(map(str, sorted(others + mem_core[1:])))
    return pins


# ---- specs ------------------------------------------------------------------------

def whole(x):
    """Exact whole number of operations (rounded half up if the split is not exact)."""
    x = Fraction(x)
    return int(x) if x.denominator == 1 else int(x + Fraction(1, 2))


def resolve_part(p, cal, pins):
    kind = p["kind"]
    def fixed_seconds():
        return float(calv(cal, p["fixed"]) if isinstance(p["fixed"], str) else cal["MECH_FIXED_S"])

    if kind == "cpu":
        d = {"kind": "cpu", "threads": p["threads"], "load": p["load"]}
        if p["fixed"]:
            d["fixed_s"] = fixed_seconds()
        elif p.get("ops_key"):
            d["ops"] = int(calv(cal, p["ops_key"]))
        else:
            d["ops"] = whole(int(cal["CPU_OPS"]) * p["frac"])
    elif kind == "vm":
        d = {"kind": "vm", "vm_bytes": f"{p['gb']}G", "method": p.get("method") or cal.get("VM_METHOD") or "all",
             "keep": bool(p["keep"]) if p.get("keep") is not None else cal.get("VM_KEEP", "0") == "1"}
        if p["fixed"]:
            d["fixed_s"] = fixed_seconds()
        else:
            d["ops"] = whole(int(cal[f"VM_OPS_{p['gb']}G"]) * p["frac"])
    elif kind == "io":
        key = p.get("bytes_key", "IO_BYTES")
        d = {"kind": "io", "pattern": p["pattern"], "bytes": int(cal[key] if key in cal else calv(cal, key)),
             "bs": "4k", "iodepth": 16}
    else:
        key = p.get("seconds_key", "IDLE_S")
        d = {"kind": "idle", "seconds": float(cal[key] if key in cal else calv(cal, key))}
    if p.get("pin"):
        if p["pin"] not in pins:
            raise ValueError(f"pin {p['pin']} not possible on this CPU topology")
        d["pin"] = pins[p["pin"]]
        if kind == "cpu" and len(parse_cpu_list(d["pin"])) != p["threads"]:
            raise ValueError(f"{p['threads']} CPU threads pinned to {len(parse_cpu_list(d['pin']))} CPUs ({d['pin']})")
    return d


def part_names(parts):
    """cpu / ram / io / idle, numbered when a kind appears more than once (RQ3: cpu1..cpu8)."""
    label = {"cpu": "cpu", "vm": "ram", "io": "io", "idle": "idle"}
    counts = {}
    for p in parts:
        counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    seen, names = {}, []
    for p in parts:
        k = p["kind"]
        seen[k] = seen.get(k, 0) + 1
        names.append(label[k] + (str(seen[k]) if counts[k] > 1 else ""))
    return names


def build_spec(row, cal, pins):
    """One schedule row -> run spec (the JSON that run_case.sh reads)."""
    case = CASE_BY_ID[row["case_id"]]
    parts = [resolve_part(p, cal, pins) for p in case["parts"]]
    for d, name in zip(parts, part_names(case["parts"])):
        d["name"] = name
    if row.get("part_order"):
        order = [int(i) for i in row["part_order"].split(",")]
        if sorted(order) != list(range(len(parts))):
            raise ValueError(f"{row['run_id']}: part_order {row['part_order']} is not a permutation")
        parts = [parts[i] for i in order]
    control = case["governor"] == "control"
    return {
        "run_id": row["run_id"], "case_id": case["case_id"], "mode": case["mode"],
        "logging": case["logging"],
        "governor": cal["GOVERNOR_CONTROL"] if control else cal["GOVERNOR_MAIN"],
        # the performance governor fixes EPP itself (intel_pstate refuses writes)
        "epp": None if control else case.get("epp") or cal["EPP_MAIN"],
        "expected_s": round(case["expected"](cal), 1),
        "block": case["block"], "rq": case["rq"],
        "round": int(row["round"]) if row.get("round") not in (None, "") else None,
        "order": int(row["order"]),
        "parts": parts,
    }


def read_schedule(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def cmd_specs(schedule_path, cal_path, env_path, out_dir):
    cal = load_calibration(cal_path)
    env = benchlib.read_env(env_path)
    if not env.get("CPU_SIBLINGS"):
        raise ValueError(f"{env_path}: CPU_SIBLINGS missing")
    pins = pin_sets(env["CPU_SIBLINGS"])
    os.makedirs(out_dir, exist_ok=True)
    rows = read_schedule(schedule_path)
    for row in rows:
        path = os.path.join(out_dir, f"{row['run_id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(build_spec(row, cal, pins), f, indent=1)
        benchlib.load_spec(path)             # same checks run_case.sh applies
    print(f"{len(rows)} run specs written to {out_dir}")
    return 0


def cmd_list():
    for c in CASES:
        kinds = "+".join(p["kind"] for p in c["parts"])
        print(f"{c['case_id']:<26} {c['block']:<5} {c['rq']:<12} {c['mode']:<10} "
              f"{'log' if c['logging'] else 'nolog':<5} {c['governor']:<7} {kinds}")
    print(f"{len(CASES)} cases (+ idle before and after the rounds)")
    return 0


def main(argv):
    try:
        if len(argv) == 6 and argv[1] == "specs":
            return cmd_specs(*argv[2:])
        if len(argv) == 2 and argv[1] == "list":
            return cmd_list()
    except (OSError, ValueError, KeyError) as e:
        print(f"cases.py: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
