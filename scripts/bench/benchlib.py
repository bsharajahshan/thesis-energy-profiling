#!/usr/bin/env python3

"""
benchlib.py - per-run logic of the fixed-work benchmark (re-run, 2026-10/11)

run_case.sh calls this twice for every measured run:

  plan   <spec.json> <session.env> <out_dir>
         Writes one small script per part (part_<i>.sh, plus prep_<i>.sh and
         clean_<i>.sh for I/O parts) and plan.json into out_dir, and prints
         plan.sh (shell assignments PLAN_*) on stdout.
  finish <spec.json> <session.env> <out_dir> [attempt max_attempts]
         Reads every file the run left in out_dir, writes result.csv (one
         row), parts.csv (one row per part) and verdict.txt, and exits 0
         (valid run) or 3 (invalid run, the caller retries it).

Standard library only: the measurement laptop has no numpy/pandas, and the
same parsers run in the tests on the development machine. Works with
Python 3.10 or newer.

Run spec (JSON, one per run, made from the schedule):
  run_id, case_id, mode ("parallel" | "sequential"), logging (bool),
  governor (str, or null = leave alone), epp (str or null), expected_s,
  optional pass-through labels: block, rq, round, order
  parts: list of
    {"name", "kind": "cpu", "threads", "ops" | "fixed_s", "load" (=100), "method" (="all"), "pin"}
    {"name", "kind": "vm", "vm_bytes" (e.g. "8G"), "workers" (=1), "ops" | "fixed_s", "method" (="all"),
     "keep" (bool, --vm-keep), "pin"}
    {"name", "kind": "io", "pattern" ("write" | "randwrite"), "bytes", "bs" (="4k"), "iodepth" (=16)}
    {"name", "kind": "idle", "seconds"}
  "ops" is the TOTAL number of bogo-ops the part must complete (all its
  workers together). How that maps to stress-ng's --cpu-ops / --vm-ops value
  depends on OPS_SEMANTICS ("total" or "per_instance"), which the pilot
  measures on the real stress-ng.

Session env (KEY='value' lines, written once per session by the preflight):
  tools     STRESS_NG FIO PERF TASKSET PYTHON FLOCK SLEEP_BIN (absolute paths)
  sysfs     RAPL_PKG_DIR RAPL_DRAM_DIR RAPL_MMIO_DIR TEMP_FILE AC_FILE CPU_SYSFS
            CGROUP_ROOT CGROUP_OURS PROC_STAT PROC_MEMINFO DISK_STAT
  other     LOCK_FILE IO_DIR PERF_EVENTS NCPU TSC_HZ
  settings  see SESSION_DEFAULTS (all optional)
"""

import csv
import json
import math
import os
import posixpath    # paths inside generated commands are Linux paths, also when tested on Windows
import re
import shlex
import sys
from datetime import datetime, timezone

EXIT_VALID = 0
EXIT_RETRY = 3

SESSION_DEFAULTS = {
    "SETTLE_TEMP_C": "55",          # start a run only at or below this package temperature
    "SETTLE_MIN_S": "5",            # ... and not before this many seconds of rest
    "SETTLE_MAX_S": "300",          # stop waiting after this; the run goes ahead, flagged
    "SETTLE_FALLBACK_S": "60",      # fixed rest when the temperature cannot be read
    "AC_WAIT_MAX_S": "3600",        # wait this long for the power cable, then fail the run
    "WATCHDOG_FACTOR": "5",         # stop a run after max(factor x expected, expected + extra)
    "WATCHDOG_MIN_EXTRA_S": "120",
    "OTHER_CPU_LIMIT": "0.10",      # CPU-seconds per second that other programs may use
    "OPS_SEMANTICS": "total",       # how stress-ng reads --cpu-ops/--vm-ops: total | per_instance
    "MEM_MARGIN_KB": str(1024 * 1024),
    "DISK_KEEP_FREE_KB": str(3 * 1024 * 1024),  # with the fio file in place, at least this much disk stays free
    "PERF_INTERVAL_MS": "1000",
    "CLK_TCK": "100",
}

KINDS = ("cpu", "vm", "io", "idle")
PASSTHROUGH = ("block", "rq", "round", "order")

RESULT_COLUMNS = [
    "run_id", "case_id", "block", "rq", "round", "order", "attempt", "max_attempts",
    "mode", "logging", "n_parts",
    "governor_planned", "epp_planned", "governor_start", "epp_start", "governor_end", "epp_end",
    "t_start_us", "t_end_us", "duration_s",
    "pkg_energy_j", "dram_energy_j", "pkg_power_w", "dram_power_w",
    "perf_pkg_energy_j", "perf_dram_energy_j",
    "cpu_ops_target", "cpu_ops_done", "vm_ops_target", "vm_ops_done",
    "io_bytes_target", "io_bytes_done", "io_iops", "disk_written_bytes",
    "instructions", "cycles", "ref_cycles", "tsc_ticks",
    "busy_cpus", "busy_mhz", "busy_mhz_min_1s", "busy_mhz_max_1s",
    "dram_read_gib", "dram_write_gib", "context_switches", "cpu_migrations", "perf_source",
    "temp_start_c", "temp_end_c", "temp_max_c", "temp_mean_c", "pkg_power_max_1s_w",
    "settle_s", "settle_timeout",
    "thr_core_count", "thr_core_ms", "thr_pkg_count", "thr_pkg_ms",
    "pl1_w_start", "pl2_w_start", "pl1_w_end", "pl2_w_end",
    "mmio_pl1_w_start", "mmio_pl2_w_start", "mmio_pl1_w_end", "mmio_pl2_w_end",
    "ac_start", "ac_end", "mem_avail_start_gib",
    "cpu_ours_s", "cpu_other_s", "cpu_total_busy_s", "other_cpu_per_s",
    "valid", "hard_reasons", "soft_flags", "finished_utc",
]

PARTS_COLUMNS = [
    "run_id", "attempt", "part_index", "part_name", "kind", "threads", "workers", "pin",
    "load_pct", "vm_bytes", "io_pattern", "fixed_s", "target", "target_unit", "flag_value",
    "done", "complete", "t_start_us", "t_end_us", "duration_s", "exit_code",
    "real_s", "usr_s", "sys_s", "ops_per_s", "stress_failed", "stress_untrustworthy",
    "fio_iops", "fio_usr_pct", "fio_sys_pct", "fio_error",
    "sub_pkg_energy_j", "sub_dram_energy_j",
]

# Names perf may print for the events this benchmark asks for.
PERF_ALIASES = {
    "instructions": ("instructions",),
    "cycles": ("cycles", "cpu-cycles"),
    "ref_cycles": ("ref-cycles",),
    "tsc": ("msr/tsc/",),
    "dram_reads": ("uncore_imc/data_reads/",),
    "dram_writes": ("uncore_imc/data_writes/",),
    "pkg_j": ("power/energy-pkg/",),
    "ram_j": ("power/energy-ram/",),
    "cs": ("context-switches", "cs"),
    "migrations": ("cpu-migrations", "migrations"),
}
PERF_UNIT_WORDS = {"", "MiB", "KiB", "GiB", "B", "Joules", "msec", "ns", "us", "Hz", "W"}


# ---- small helpers ----------------------------------------------------------

def read_text(path):
    """File content, or "" if it does not exist or cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def to_int(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def to_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def to_us(s):
    """bash $EPOCHREALTIME ("1700000000.123456", "," under some locales) -> microseconds."""
    s = str(s).strip().replace(",", ".")
    sec, _, frac = s.partition(".")
    if not sec.isdigit() or (frac and not frac.isdigit()):
        raise ValueError(f"not an epoch time: {s!r}")
    return int(sec) * 1_000_000 + int((frac + "000000")[:6])


def fmt(v):
    """CSV cell: '' for missing, plain integers, floats with up to 6 decimals."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        return f"{v:.6f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def fmt_num(x):
    """60.0 -> '60', 0.5 -> '0.5' (for command-line values)."""
    x = float(x)
    return str(int(x)) if x == int(x) else repr(x)


def size_to_bytes(s):
    """'8G', '512M', '64K', '1024' -> bytes (powers of 1024, as stress-ng reads them)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt]?)[Bb]?", str(s).strip())
    if not m:
        raise ValueError(f"bad size: {s!r}")
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def read_env(path):
    """Session env file (KEY='value' lines, shell quoting) -> dict."""
    env = {}
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        try:
            words = shlex.split(raw, posix=True)
        except ValueError:
            words = [raw]
        env[key] = words[0] if words else ""
    return env


def setting(env, key):
    v = env.get(key, "")
    return v if v != "" else SESSION_DEFAULTS.get(key, "")


# ---- run spec and plan ------------------------------------------------------

def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    problems = []
    for key in ("run_id", "case_id", "mode", "parts", "expected_s"):
        if key not in spec:
            problems.append(f"missing {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(spec.get("run_id", ""))):
        problems.append("run_id may only use letters, digits, '_', '.', '-'")
    if spec.get("mode") not in ("parallel", "sequential"):
        problems.append(f"bad mode {spec.get('mode')!r}")
    parts = spec.get("parts") or []
    if not parts:
        problems.append("no parts")
    for i, p in enumerate(parts):
        try:
            kind = p.get("kind")
            if kind not in KINDS:
                problems.append(f"part {i}: bad kind {kind!r}")
                continue
            if kind in ("cpu", "vm"):
                if (p.get("ops") is None) == (p.get("fixed_s") is None):
                    problems.append(f"part {i}: give exactly one of ops / fixed_s")
                if p.get("ops") is not None and int(p["ops"]) < 1:
                    problems.append(f"part {i}: ops must be >= 1")
            if kind == "cpu" and int(p.get("threads", 0)) < 1:
                problems.append(f"part {i}: threads must be >= 1")
            if kind == "vm":
                size_to_bytes(p.get("vm_bytes", ""))
            if kind == "io":
                if p.get("pattern") not in ("write", "randwrite"):
                    problems.append(f"part {i}: pattern must be write or randwrite")
                if int(p.get("bytes", 0)) < 1:
                    problems.append(f"part {i}: bytes must be >= 1")
            if kind == "idle" and float(p.get("seconds", 0)) <= 0:
                problems.append(f"part {i}: seconds must be > 0")
        except (TypeError, ValueError) as e:
            problems.append(f"part {i}: {e}")
    try:
        if float(spec.get("expected_s", 0)) <= 0:
            problems.append("expected_s must be > 0")
    except (TypeError, ValueError):
        problems.append("expected_s must be a number")
    if problems:
        raise ValueError("; ".join(problems))
    return spec


def watchdog_seconds(expected_s, env):
    factor = float(setting(env, "WATCHDOG_FACTOR"))
    extra = float(setting(env, "WATCHDOG_MIN_EXTRA_S"))
    return int(math.ceil(max(expected_s * factor, expected_s + extra)))


def ops_flag_value(total_ops, instances, semantics):
    """(value for --cpu-ops/--vm-ops, total ops that value really asks for)."""
    if semantics == "per_instance":
        per = -(-total_ops // instances)          # ceiling division
        return per, per * instances
    return total_ops, total_ops


def shell_script(argv, comment):
    return "#!/bin/bash\n# " + comment + "\nexec " + " ".join(shlex.quote(str(a)) for a in argv) + "\n"


def build_plan(spec, env, out_dir):
    """-> (plan dict, {file name: content}, plan.sh text). Pure: writes nothing."""
    expected = float(spec["expected_s"])
    watchdog = watchdog_seconds(expected, env)
    safety = max(10, int(watchdog * 0.9))         # tools stop themselves before the watchdog
    semantics = setting(env, "OPS_SEMANTICS")
    io_dir = env.get("IO_DIR") or out_dir
    run_id = spec["run_id"]
    parts, files, mems, disks = [], {}, [], []

    for i, p in enumerate(spec["parts"]):
        kind = p["kind"]
        e = {"index": i, "name": p.get("name") or f"{kind}{i}", "kind": kind,
             "threads": None, "workers": None, "pin": str(p.get("pin") or ""), "load": None,
             "vm_bytes": None, "pattern": None, "fixed_s": None, "target": None,
             "target_unit": None, "flag_value": None, "io_file": None}
        prefix = [env["TASKSET"], "-c", e["pin"]] if e["pin"] else []
        prep = clean = None

        if kind in ("cpu", "vm"):
            if kind == "cpu":
                n = int(p["threads"])
                load = int(p.get("load", 100))
                argv = [env["STRESS_NG"], "--cpu", str(n), "--cpu-method", p.get("method") or "all"]
                if load < 100:
                    argv += ["--cpu-load", str(load)]
                e.update(threads=n, load=load)
            else:
                n = int(p.get("workers", 1))
                vm_bytes = str(p["vm_bytes"])
                argv = [env["STRESS_NG"], "--vm", str(n), "--vm-bytes", vm_bytes,
                        "--vm-method", p.get("method") or "all"]
                if p.get("keep"):
                    argv.append("--vm-keep")        # map the buffer once, not again for every operation
                e.update(workers=n, vm_bytes=vm_bytes)
                mems.append(size_to_bytes(vm_bytes) * n)
            if p.get("fixed_s") is not None:
                fs = float(p["fixed_s"])
                argv += ["--timeout", fmt_num(fs) + "s"]
                e.update(fixed_s=fs, target=fs, target_unit="s")
            else:
                flag, target = ops_flag_value(int(p["ops"]), n, semantics)
                argv += [f"--{kind}-ops", str(flag), "--timeout", f"{safety}s"]
                e.update(target=target, target_unit="ops", flag_value=flag)
            argv.append("--metrics-brief")

        elif kind == "io":
            nbytes = int(p["bytes"])
            pattern = p["pattern"]
            path = posixpath.join(io_dir, f"fio_{run_id}_p{i}.dat")
            common = [env["FIO"], "--name=io_job", f"--filename={path}", f"--size={nbytes}",
                      f"--bs={p.get('bs') or '4k'}", "--ioengine=libaio",
                      f"--iodepth={int(p.get('iodepth', 16))}", f"--rw={pattern}", "--randrepeat=1"]
            prep = common + ["--create_only=1"]
            argv = common + [f"--io_size={nbytes}", "--end_fsync=1", f"--runtime={safety}",
                             "--output-format=json", f"--output={posixpath.join(out_dir, f'part_{i}.json')}"]
            clean = ["rm", "-f", path]
            e.update(pattern=pattern, target=nbytes, target_unit="bytes", io_file=path)
            disks.append(nbytes)

        else:  # idle
            secs = float(p["seconds"])
            argv = [env.get("SLEEP_BIN") or "sleep", fmt_num(secs)]
            e.update(fixed_s=secs, target=secs, target_unit="s")

        e["argv"] = prefix + argv
        label = f"run {run_id}, part {i} ({e['name']}), generated by benchlib.py plan"
        files[f"part_{i}.sh"] = shell_script(e["argv"], label)
        if prep:
            files[f"prep_{i}.sh"] = shell_script(prep, label + " - creates the fio file before the window")
        if clean:
            files[f"clean_{i}.sh"] = shell_script(clean, label + " - deletes the fio file after the window")
        parts.append(e)

    combine = sum if spec["mode"] == "parallel" else max
    need_mem_kb = (combine(mems) if mems else 0) // 1024 + int(setting(env, "MEM_MARGIN_KB"))
    need_disk_kb = ((combine(disks) // 1024 + int(setting(env, "DISK_KEEP_FREE_KB"))) if disks else 0)

    plan = {
        "run_id": run_id, "case_id": spec["case_id"], "mode": spec["mode"],
        "logging": bool(spec.get("logging", True)),
        "governor": spec.get("governor") or "", "epp": spec.get("epp") or "",
        "expected_s": expected, "watchdog_s": watchdog, "io_dir": io_dir,
        "need_mem_kb": need_mem_kb, "need_disk_kb": need_disk_kb, "parts": parts,
        "labels": {k: spec.get(k) for k in PASSTHROUGH},
    }
    sh = {
        "PLAN_RUN_ID": run_id,
        "PLAN_MODE": spec["mode"],
        "PLAN_NPARTS": len(parts),
        "PLAN_LOGGING": 1 if plan["logging"] else 0,
        "PLAN_GOVERNOR": plan["governor"],
        "PLAN_EPP": plan["epp"],
        "PLAN_EXPECTED_S": fmt_num(expected),
        "PLAN_WATCHDOG_S": watchdog,
        "PLAN_NEED_MEM_KB": need_mem_kb,
        "PLAN_NEED_DISK_KB": need_disk_kb,
        "PLAN_IO_DIR": io_dir,
        "PLAN_SETTLE_TEMP_C": int(float(setting(env, "SETTLE_TEMP_C"))),
        "PLAN_SETTLE_MIN_S": int(float(setting(env, "SETTLE_MIN_S"))),
        "PLAN_SETTLE_MAX_S": int(float(setting(env, "SETTLE_MAX_S"))),
        "PLAN_SETTLE_FALLBACK_S": int(float(setting(env, "SETTLE_FALLBACK_S"))),
        "PLAN_AC_WAIT_MAX_S": int(float(setting(env, "AC_WAIT_MAX_S"))),
        "PLAN_PERF_INTERVAL_MS": int(float(setting(env, "PERF_INTERVAL_MS"))),
    }
    plan_sh = "# plan for run " + run_id + ", generated by benchlib.py plan\n" + "".join(
        f"{k}={shlex.quote(str(v))}\n" for k, v in sh.items())
    return plan, files, plan_sh


def cmd_plan(spec_path, env_path, out_dir):
    try:
        spec = load_spec(spec_path)
        env = read_env(env_path)
        plan, files, plan_sh = build_plan(spec, env, os.path.abspath(out_dir))
    except (OSError, ValueError, KeyError) as e:
        print(f"benchlib plan: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for name, text in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(os.path.join(out_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    sys.stdout.write(plan_sh)
    return 0


# ---- parsers ----------------------------------------------------------------

_STRESS_LINE = re.compile(
    r"stress-ng:\s+(?:metrc|info):\s+\[\d+\]\s+([A-Za-z][A-Za-z0-9_-]*)\s+(\d+)\s+"
    r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)")


def parse_stress(text):
    """stress-ng --metrics-brief output -> per-stressor counters and the run summary.

    Checked against real stress-ng 0.17.06 output ('metrc:' tag); older
    versions print the same columns with an 'info:' tag."""
    out = {"stressors": {}, "passed": None, "failed": None, "skipped": None,
           "untrustworthy": None, "completed": None}
    for line in text.splitlines():
        m = _STRESS_LINE.search(line)
        if m and m.group(1) != "stressor":
            out["stressors"][m.group(1)] = {
                "bogo_ops": int(m.group(2)), "real_s": to_float(m.group(3)),
                "usr_s": to_float(m.group(4)), "sys_s": to_float(m.group(5)),
                "ops_per_s": to_float(m.group(6)), "ops_per_cpu_s": to_float(m.group(7))}
            continue
        for key, pattern in (("passed", r"\]\s+passed:\s+(\d+)"), ("failed", r"\]\s+failed:\s+(\d+)"),
                             ("skipped", r"\]\s+skipped:\s+(\d+)"),
                             ("untrustworthy", r"metrics untrustworthy:\s+(\d+)")):
            m = re.search(pattern, line)
            if m:
                out[key] = int(m.group(1))
        m = re.search(r"\b(successful|unsuccessful) run completed", line)
        if m:
            out["completed"] = m.group(1) == "successful"
    return out


def parse_fio_json(text):
    """fio --output-format=json -> write counters of the first job (checked against fio 3.42)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in fio output")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    job = data["jobs"][0]
    w = job.get("write", {})
    util = (data.get("disk_util") or [{}])[0]
    return {"version": data.get("fio version"), "error": job.get("error"),
            "io_bytes": w.get("io_bytes"), "iops": w.get("iops"), "runtime_ms": w.get("runtime"),
            "total_ios": w.get("total_ios"), "usr_cpu_pct": job.get("usr_cpu"),
            "sys_cpu_pct": job.get("sys_cpu"), "job_runtime_ms": job.get("job_runtime"),
            "disk": util.get("name"), "disk_util_pct": util.get("util")}


def _perf_event_index(f, wanted):
    """Field index of the event name: 2 = value,unit,event,... ; 3 = time,value,unit,event,..."""
    for k in (3, 2):
        if len(f) > k and f[k] in wanted:
            return k
    for k in (3, 2):
        if len(f) > k:
            name = f[k]
            if (name and to_float(name) is None and name not in PERF_UNIT_WORDS
                    and not name.startswith("<") and re.search(r"[a-z]", name)):
                return k
    return None


def parse_perf_csv(text, wanted=()):
    """perf stat -x, output -> totals per event and the per-interval series.

    Handles plain mode (value,unit,event,run time,pct,...) as printed by
    perf 7.0 on the laptop (real file in the tests), and interval mode
    (-I: time,value,unit,event,...) with --summary lines, prefixed 'summary'
    or not. The interval format is taken from the perf documentation and is
    confirmed only by the pilot. '<not counted>' / '<not supported>' give
    value None."""
    wanted = {w.strip() for w in wanted if w and w.strip()}
    summary, plain, series = {}, {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = [x.strip() for x in line.split(",")]
        prefixed = f[0] == "summary"
        if prefixed:
            f = f[1:]
        k = _perf_event_index(f, wanted)
        if k is None:
            continue
        if k == 3:
            t, vstr, unit, rest = to_float(f[0]), f[1], f[2], f[4:]
        else:
            t, vstr, unit, rest = None, f[0], f[1], f[3:]
        rec = {"value": to_float(vstr), "unit": unit, "raw": vstr,
               "pct": to_float(rest[1]) if len(rest) > 1 else None}
        event = f[k]
        if prefixed or (k == 3 and t is None):
            summary[event] = rec
        elif k == 3:
            series.append((t, event, rec["value"], unit, rec["pct"]))
        else:
            plain[event] = rec
    if summary:
        return {"totals": summary, "series": series, "source": "summary"}
    if plain:
        return {"totals": plain, "series": series, "source": "plain"}
    totals = {}
    for t, event, value, unit, pct in series:
        rec = totals.setdefault(event, {"value": 0.0, "unit": unit, "raw": "", "pct": pct})
        if value is None:
            rec["value"], rec["raw"] = None, "<not counted>"
        elif rec["value"] is not None:
            rec["value"] += value
        if pct is not None and (rec["pct"] is None or pct < rec["pct"]):
            rec["pct"] = pct
    return {"totals": totals, "series": series, "source": "intervals" if series else ""}


def perf_lookup(table, key):
    for name in PERF_ALIASES[key]:
        for variant in (name, name.rstrip("/")):
            if variant in table:
                return table[variant]
    return None


def to_gib(rec):
    """uncore_imc counter record -> GiB (perf prints MiB; raw counts are 64-byte lines)."""
    if not rec or rec["value"] is None:
        return None
    unit = rec["unit"]
    scale = {"MiB": 1 / 1024, "GiB": 1.0, "KiB": 1 / 1024 ** 2, "B": 1 / 1024 ** 3}.get(unit)
    if scale is None:
        scale = 64 / 1024 ** 3 if unit == "" else None
    return rec["value"] * scale if scale is not None else None


def parse_sampler(text):
    """sampler.sh lines 'time_us pkg_uj dram_uj temp_mC' ('-' = not readable)."""
    rows = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        try:
            rows.append((to_us(f[0]), to_int(f[1]), to_int(f[2]), to_int(f[3])))
        except ValueError:
            continue
    return rows


def parse_snap(text):
    """window.sh snapshot (snap_before.txt / snap_after.txt) -> dict."""
    s = {"gov": {}, "epp": {}, "thr_core": {}, "lim": {}, "cg": {}, "procstat": None, "disk": None}
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        key = f[0]
        if key == "time_us" and len(f) > 1:
            try:
                s["time_us"] = to_us(f[1])
            except ValueError:
                pass
        elif key in ("pkg_uj", "dram_uj", "pkg_max_uj", "dram_max_uj", "temp_mc", "ac",
                     "memavail_kb", "thr_pkg_count", "thr_pkg_ms") and len(f) > 1:
            s[key] = to_int(f[1])
        elif key in ("gov", "epp") and len(f) > 2:
            s[key][f[1]] = f[2]
        elif key == "thr_core" and len(f) > 3:
            s["thr_core"][f[1]] = (to_int(f[2]), to_int(f[3]))
        elif key == "lim" and len(f) > 4:
            s["lim"][(f[1], f[3])] = to_int(f[4])
        elif key == "cg" and len(f) > 2:
            s["cg"][f[1]] = to_int(f[2])
        elif key == "procstat" and len(f) > 2:
            s["procstat"] = [to_int(x) for x in f[2:]]
        elif key == "disk" and len(f) > 1:
            s["disk"] = [to_int(x) for x in f[1:]]
    return s


def parse_window(text):
    w = {"start": None, "end": None, "sub": {}}
    for line in text.splitlines():
        f = line.split()
        try:
            if f and f[0] in ("win_start", "win_end") and len(f) >= 4:
                w["start" if f[0] == "win_start" else "end"] = (to_us(f[1]), to_int(f[2]), to_int(f[3]))
            elif f and f[0] in ("sub_start", "sub_end") and len(f) >= 5:
                side = "start" if f[0] == "sub_start" else "end"
                w["sub"].setdefault(int(f[1]), {})[side] = (to_us(f[2]), to_int(f[3]), to_int(f[4]))
        except ValueError:
            continue
    return w


def parse_events(text):
    events = []
    for line in text.splitlines():
        f = line.split()
        if f:
            events.append((f[0], f[1:]))
    return events


def parse_part_time(text):
    f = text.split()
    if len(f) < 3:
        return None
    try:
        return to_us(f[0]), to_us(f[1]), to_int(f[2])
    except ValueError:
        return None


def parse_keyvals(text):
    d = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2:
            d[f[0]] = f[1]
    return d


# ---- evaluation -------------------------------------------------------------

def energy_delta(before, after, max_range):
    """Counter difference in uJ; one wraparound is corrected with the domain's own range."""
    if before is None or after is None:
        return None
    d = after - before
    if d < 0:
        if not max_range:
            return None
        d += max_range
    return d


def _uniform(values):
    vals = sorted(set(values.values()))
    if not vals:
        return None
    return vals[0] if len(vals) == 1 else "mixed:" + "|".join(vals)


def _limit_w(snap, tree, name):
    uw = snap["lim"].get((tree, name))
    return uw / 1e6 if uw is not None else None


def _delta(a, b):
    return b - a if a is not None and b is not None else None


def evaluate(plan, out_dir, env, attempt, max_attempts):
    """Read the run's files -> (result row dict, list of part row dicts, hard reasons, soft flags)."""
    hard, soft = [], []
    p_ = lambda name: os.path.join(out_dir, name)  # noqa: E731

    events = parse_events(read_text(p_("events.txt")))
    event_names = [e[0] for e in events]
    for name in ("plan_failed", "lock_busy", "watchdog_fired", "prep_failed", "ac_timeout",
                 "mem_low", "disk_low", "stopped_by_signal"):
        if name in event_names:
            hard.append(name)
    for name in ("settle_timeout", "temp_unreadable", "perf_fallback",
                 "governor_set_failed", "epp_set_failed"):
        if name in event_names:
            soft.append(name)

    win = parse_window(read_text(p_("window.txt")))
    before = parse_snap(read_text(p_("snap_before.txt")))
    after = parse_snap(read_text(p_("snap_after.txt")))
    settle = parse_keyvals(read_text(p_("settle.txt")))

    row = {c: None for c in RESULT_COLUMNS}
    row.update(run_id=plan["run_id"], case_id=plan["case_id"], attempt=attempt,
               max_attempts=max_attempts, mode=plan["mode"], logging=plan["logging"],
               n_parts=len(plan["parts"]), governor_planned=plan["governor"] or None,
               epp_planned=plan["epp"] or None)
    for k, v in plan.get("labels", {}).items():
        row[k] = v

    # -- window and energy
    duration = None
    if win["start"] and win["end"]:
        t0, pkg0, dram0 = win["start"]
        t1, pkg1, dram1 = win["end"]
        duration = (t1 - t0) / 1e6
        row.update(t_start_us=t0, t_end_us=t1, duration_s=duration)
        pkg = energy_delta(pkg0, pkg1, before.get("pkg_max_uj"))
        dram = energy_delta(dram0, dram1, before.get("dram_max_uj"))
        row["pkg_energy_j"] = pkg / 1e6 if pkg is not None else None
        row["dram_energy_j"] = dram / 1e6 if dram is not None else None
        if duration > 0:
            if pkg is not None:
                row["pkg_power_w"] = pkg / 1e6 / duration
            if dram is not None:
                row["dram_power_w"] = dram / 1e6 / duration
    elif "lock_busy" not in hard:
        hard.append("window_incomplete")
    if duration is not None and duration <= 0:
        hard.append("duration_not_positive")
    if duration is not None:
        if not row["pkg_energy_j"] or row["pkg_energy_j"] <= 0:
            hard.append("pkg_energy_missing")
        if env.get("RAPL_DRAM_DIR") and (not row["dram_energy_j"] or row["dram_energy_j"] <= 0):
            hard.append("dram_energy_missing")

    # -- parts (their own checks only if the window started; otherwise the
    #    window-level reason above already says why nothing ran)
    window_ran = win["start"] is not None
    part_rows = []
    ops_target = {"cpu": None, "vm": None}       # sums over ops-based parts
    ops_done = {"cpu": None, "vm": None}         # sums over parts whose counters were read
    io_target = io_done = io_iops = None
    for pp in plan["parts"]:
        i = pp["index"]
        r = {c: None for c in PARTS_COLUMNS}
        r.update(run_id=plan["run_id"], attempt=attempt, part_index=i, part_name=pp["name"],
                 kind=pp["kind"], threads=pp["threads"], workers=pp["workers"], pin=pp["pin"] or None,
                 load_pct=pp["load"], vm_bytes=pp["vm_bytes"], io_pattern=pp["pattern"],
                 fixed_s=pp["fixed_s"], target=pp["target"], target_unit=pp["target_unit"],
                 flag_value=pp["flag_value"])
        tm = parse_part_time(read_text(p_(f"part_{i}.time")))
        dur = None
        if tm:
            r.update(t_start_us=tm[0], t_end_us=tm[1], exit_code=tm[2])
            dur = (tm[1] - tm[0]) / 1e6
            r["duration_s"] = dur
        complete = False
        if pp["kind"] in ("cpu", "vm"):
            st = parse_stress(read_text(p_(f"part_{i}.out")))
            m = st["stressors"].get(pp["kind"])
            r.update(stress_failed=st["failed"], stress_untrustworthy=st["untrustworthy"])
            if m:
                r.update(done=m["bogo_ops"], real_s=m["real_s"], usr_s=m["usr_s"], sys_s=m["sys_s"],
                         ops_per_s=m["ops_per_s"])
                ops_done[pp["kind"]] = (ops_done[pp["kind"]] or 0) + m["bogo_ops"]
            if pp["target_unit"] == "ops":
                ops_target[pp["kind"]] = (ops_target[pp["kind"]] or 0) + pp["target"]
                complete = m is not None and m["bogo_ops"] >= pp["target"]
            else:
                complete = dur is not None and dur >= 0.95 * pp["fixed_s"]
            if st["failed"]:
                hard.append(f"stress_failed_p{i}")
            if st["untrustworthy"]:
                soft.append(f"stress_untrustworthy_p{i}")
        elif pp["kind"] == "io":
            try:
                fio = parse_fio_json(read_text(p_(f"part_{i}.json")))
            except (ValueError, KeyError, IndexError) as e:
                fio = None
                soft.append(f"fio_json_unreadable_p{i}")
                print(f"benchlib finish: fio output of part {i}: {e}", file=sys.stderr)
            if fio:
                r.update(done=fio["io_bytes"], fio_iops=fio["iops"], fio_usr_pct=fio["usr_cpu_pct"],
                         fio_sys_pct=fio["sys_cpu_pct"], fio_error=fio["error"])
                complete = (fio["io_bytes"] or 0) >= pp["target"] and fio["error"] == 0
                io_done = (io_done or 0) + (fio["io_bytes"] or 0)
                io_iops = (io_iops or 0) + (fio["iops"] or 0)
            io_target = (io_target or 0) + pp["target"]
        else:  # idle
            r["done"] = dur
            complete = dur is not None and dur >= 0.95 * pp["fixed_s"]
        if window_ran:
            if tm is None:
                hard.append(f"part_not_run_p{i}")
            elif tm[2] != 0:
                hard.append(f"part_exit_{tm[2]}_p{i}")
            if not complete:
                hard.append(f"work_incomplete_p{i}")
        r["complete"] = complete
        sub = win["sub"].get(i, {})
        if "start" in sub and "end" in sub:
            sp = energy_delta(sub["start"][1], sub["end"][1], before.get("pkg_max_uj"))
            sd = energy_delta(sub["start"][2], sub["end"][2], before.get("dram_max_uj"))
            r["sub_pkg_energy_j"] = sp / 1e6 if sp is not None else None
            r["sub_dram_energy_j"] = sd / 1e6 if sd is not None else None
        part_rows.append(r)
    row.update(cpu_ops_target=ops_target["cpu"], cpu_ops_done=ops_done["cpu"],
               vm_ops_target=ops_target["vm"], vm_ops_done=ops_done["vm"],
               io_bytes_target=io_target, io_bytes_done=io_done, io_iops=io_iops)

    # -- perf
    if plan["logging"]:
        text = read_text(p_("perf.csv"))
        wanted = [e for e in env.get("PERF_EVENTS", "").split(",") if e]
        perf = parse_perf_csv(text, wanted) if text else {"totals": {}, "series": [], "source": ""}
        tot = perf["totals"]
        row["perf_source"] = perf["source"] or None
        if not tot:
            soft.append("perf_missing")
        else:
            if perf["source"] == "intervals":
                soft.append("perf_total_from_intervals")
            for event, rec in tot.items():
                if rec["value"] is None:
                    soft.append(f"perf_not_counted:{event}")
                elif rec["pct"] is not None and rec["pct"] < 99.0:
                    soft.append(f"perf_multiplexed:{event}")

            def val(key):
                rec = perf_lookup(tot, key)
                return rec["value"] if rec else None

            row.update(instructions=val("instructions"), cycles=val("cycles"),
                       ref_cycles=val("ref_cycles"), tsc_ticks=val("tsc"),
                       context_switches=val("cs"), cpu_migrations=val("migrations"),
                       perf_pkg_energy_j=val("pkg_j"), perf_dram_energy_j=val("ram_j"),
                       dram_read_gib=to_gib(perf_lookup(tot, "dram_reads")),
                       dram_write_gib=to_gib(perf_lookup(tot, "dram_writes")))
            ncpu = to_int(env.get("NCPU")) or os.cpu_count() or 1
            tsc_hz = to_float(env.get("TSC_HZ"))
            if not tsc_hz and row["tsc_ticks"] and duration:
                tsc_hz = row["tsc_ticks"] / ncpu / duration
            if tsc_hz and row["ref_cycles"] and duration:
                row["busy_cpus"] = row["ref_cycles"] / tsc_hz / duration
                if row["cycles"]:
                    row["busy_mhz"] = row["cycles"] / row["ref_cycles"] * tsc_hz / 1e6
            if tsc_hz and perf["series"]:
                per_t = {}
                for t, event, value, _unit, _pct in perf["series"]:
                    per_t.setdefault(t, {})[event] = value
                mhz = []
                step = int(setting(env, "PERF_INTERVAL_MS")) / 1000
                for vals in per_t.values():
                    cyc = perf_lookup({k: {"value": v} for k, v in vals.items()}, "cycles")
                    ref = perf_lookup({k: {"value": v} for k, v in vals.items()}, "ref_cycles")
                    if cyc and ref and cyc["value"] and ref["value"] and ref["value"] > 0.1 * tsc_hz * step:
                        mhz.append(cyc["value"] / ref["value"] * tsc_hz / 1e6)
                if mhz:
                    row.update(busy_mhz_min_1s=min(mhz), busy_mhz_max_1s=max(mhz))

        samples = parse_sampler(read_text(p_("sampler.txt")))
        if not samples:
            soft.append("sampler_missing")
        elif row["t_start_us"] is not None:
            inside = [s for s in samples if row["t_start_us"] <= s[0] <= row["t_end_us"]]
            temps = [s[3] / 1000 for s in inside if s[3] is not None]
            if temps:
                row.update(temp_max_c=max(temps), temp_mean_c=sum(temps) / len(temps))
            powers = []
            for a, b in zip(inside, inside[1:]):
                de = energy_delta(a[1], b[1], before.get("pkg_max_uj"))
                if de is not None and b[0] > a[0]:
                    powers.append(de / (b[0] - a[0]))      # uJ/us = W
            if powers:
                row["pkg_power_max_1s_w"] = max(powers)

    # -- machine state before / after
    row["temp_start_c"] = before["temp_mc"] / 1000 if before.get("temp_mc") is not None else None
    row["temp_end_c"] = after["temp_mc"] / 1000 if after.get("temp_mc") is not None else None
    row["settle_s"] = to_int(settle.get("settle_s"))
    row["settle_timeout"] = "settle_timeout" in event_names
    core_b, core_a = before["thr_core"], after["thr_core"]
    if core_b and core_a:
        row["thr_core_count"] = sum((core_a[c][0] or 0) - (core_b[c][0] or 0) for c in core_b if c in core_a)
        row["thr_core_ms"] = sum((core_a[c][1] or 0) - (core_b[c][1] or 0) for c in core_b if c in core_a)
    row["thr_pkg_count"] = _delta(before.get("thr_pkg_count"), after.get("thr_pkg_count"))
    row["thr_pkg_ms"] = _delta(before.get("thr_pkg_ms"), after.get("thr_pkg_ms"))
    for snap, side in ((before, "start"), (after, "end")):
        row[f"pl1_w_{side}"] = _limit_w(snap, "pkg", "long_term")
        row[f"pl2_w_{side}"] = _limit_w(snap, "pkg", "short_term")
        row[f"mmio_pl1_w_{side}"] = _limit_w(snap, "mmio", "long_term")
        row[f"mmio_pl2_w_{side}"] = _limit_w(snap, "mmio", "short_term")
        row[f"governor_{side}"] = _uniform(snap["gov"])
        row[f"epp_{side}"] = _uniform(snap["epp"])
        row[f"ac_{side}"] = snap.get("ac")
    if before.get("memavail_kb") is not None:
        row["mem_avail_start_gib"] = before["memavail_kb"] / 1024 ** 2

    if env.get("AC_FILE") and duration is not None and (row["ac_start"] != 1 or row["ac_end"] != 1):
        hard.append("on_battery")
    if plan["governor"] and duration is not None:
        if row["governor_start"] != plan["governor"]:
            hard.append("governor_not_as_planned")
        elif row["governor_end"] != plan["governor"]:
            soft.append("governor_changed_during_run")
    if plan["epp"] and duration is not None:
        if row["epp_start"] != plan["epp"]:
            hard.append("epp_not_as_planned")
        elif row["epp_end"] != plan["epp"]:
            soft.append("epp_changed_during_run")

    # -- disk and CPU time of other programs
    if before["disk"] and after["disk"] and len(before["disk"]) > 6 and len(after["disk"]) > 6:
        sectors = _delta(before["disk"][6], after["disk"][6])
        row["disk_written_bytes"] = sectors * 512 if sectors is not None else None
    if before["procstat"] and after["procstat"] and len(before["procstat"]) >= 8:
        busy = lambda v: v[0] + v[1] + v[2] + v[5] + v[6] + v[7]  # noqa: E731  user nice system irq softirq steal
        row["cpu_total_busy_s"] = (busy(after["procstat"]) - busy(before["procstat"])) / int(setting(env, "CLK_TCK"))
    cg = {k: _delta(before["cg"].get(k), after["cg"].get(k)) for k in ("ours", "user", "system", "init")}
    if duration and cg["ours"] is not None and cg["user"] is not None and cg["system"] is not None:
        other_user = cg["user"] - cg["ours"] if "/user.slice/" in env.get("CGROUP_OURS", "") else cg["user"]
        other = other_user + cg["system"] + (cg["init"] or 0)
        row.update(cpu_ours_s=cg["ours"] / 1e6, cpu_other_s=other / 1e6, other_cpu_per_s=other / 1e6 / duration)
        if row["other_cpu_per_s"] > float(setting(env, "OTHER_CPU_LIMIT")):
            if attempt < max_attempts:
                hard.append("disturbed")
            else:
                soft.append("disturbed_kept_last_attempt")
    elif duration:
        soft.append("no_cgroup_data")

    row["valid"] = not hard
    row["hard_reasons"] = ";".join(hard) or None
    row["soft_flags"] = ";".join(soft) or None
    row["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return row, part_rows, hard, soft


def write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow([fmt(r.get(c)) for c in columns])


def cmd_finish(spec_path, env_path, out_dir, attempt=1, max_attempts=1):
    env = read_env(env_path)
    try:
        with open(os.path.join(out_dir, "plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        print(f"benchlib finish: no plan.json: {e}", file=sys.stderr)
        with open(os.path.join(out_dir, "verdict.txt"), "w", encoding="utf-8") as f:
            f.write("RETRY\nhard: plan_missing\n")
        return EXIT_RETRY
    row, part_rows, hard, soft = evaluate(plan, out_dir, env, int(attempt), int(max_attempts))
    write_csv(os.path.join(out_dir, "result.csv"), RESULT_COLUMNS, [row])
    write_csv(os.path.join(out_dir, "parts.csv"), PARTS_COLUMNS, part_rows)
    with open(os.path.join(out_dir, "verdict.txt"), "w", encoding="utf-8") as f:
        f.write(("VALID" if not hard else "RETRY") + "\n")
        f.write("hard: " + (", ".join(hard) or "-") + "\n")
        f.write("soft: " + (", ".join(soft) or "-") + "\n")
    return EXIT_VALID if not hard else EXIT_RETRY


def main(argv):
    if len(argv) == 5 and argv[1] == "plan":
        return cmd_plan(argv[2], argv[3], argv[4])
    if len(argv) in (5, 7) and argv[1] == "finish":
        return cmd_finish(*argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
