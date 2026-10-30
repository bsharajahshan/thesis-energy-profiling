#!/usr/bin/env python3

"""
calibrate.py - job sizes from the pilot's probe runs

session.sh runs it once in the pilot, after the probe phase. It reads the
valid probe runs and writes into the session folder:
  calibration.env              the pilot's calibration: the session's copy with the
                               measured numbers replaced (a header says so); the
                               rest of the pilot (repeats, rehearsal) uses it
  calibration_before_pilot.env the copy as it was before
  calibration_report.txt       every number and how it was found
A number that could not be measured keeps its old value; the report says so.
After the pilot the numbers are reviewed and copied into
scripts/bench/calibration.env for the main session (by hand, committed).

  OPS_SEMANTICS        probe_ops_semantics: operations done / operations asked
                       (about 1 = total for all workers, about 4 = per worker)
  CPU_OPS              speed with 8 threads x TARGET seconds, multiple of 800
  CPU_S_T1/2/4/8,      CPU_OPS / speed at that thread count or load
  CPU_S_T8_LOAD50/25
  VM_OPS_<n>G          the same DRAM traffic for every buffer size: traffic per
                       operation of probe_mem_<n>g (perf uncore_imc reads + writes,
                       minus the idle traffic of probe_idle); target = traffic of
                       the 8 GB job in TARGET seconds; multiple of 8
  VM_S                 the longest memory job (watchdog and estimate only)
  VM_METHOD, VM_KEEP   memory check only (probe_vmm_<method>_<n>g): the vm method whose
                       data per operation is the same at all four sizes (smallest spread)
                       ('all' kept if within MEMCHECK_KEEP_ALL_PCT points), --vm-keep;
                       then VM_OPS_<n>G as above with that method
  Only the parts whose probes are in the session's schedule are reported.
  IO_BYTES             sequential write speed x TARGET seconds, whole MiB
  IO_S_SEQ, IO_S_RAND  IO_BYTES / speed
  SETTLE_TEMP_C        temperature 60 s after the end of 60 s with 8 CPU threads
                       (probe_cooldown), rounded up; at least idle + 3 C, at most 75 C
  OTHER_CPU_LIMIT      2 x the CPU of other programs during probe_idle, between 0.10 and 0.5
  EPP_MAIN             the EPP the start check found after setting the power profile
Speeds are operations (or bytes) divided by the wall time of the job, start-up included.

Usage: calibrate.py <session> [target seconds, default 60]     (exit 0 = written, 1 = nothing written)
"""

import csv
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import benchlib  # noqa: E402
import cases  # noqa: E402

MIB = 1024 ** 2


def num(v):
    return benchlib.to_float(v) if v not in (None, "") else None


def read_rows(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def probe(session, run_id):
    """The valid attempt of a probe run: {'dir', 'res' (result row), 'parts'} or None."""
    att = benchlib.read_text(os.path.join(session, "runs", run_id, "VALID")).strip()
    if not att:
        return None
    d = os.path.join(session, "runs", run_id, att)
    res = read_rows(os.path.join(d, "result.csv"))
    return {"dir": d, "res": res[0] if res else {}, "parts": read_rows(os.path.join(d, "parts.csv"))}


def part_speed(p, i=0):
    """work done / wall time of part i (operations or bytes per second)."""
    if not p or len(p["parts"]) <= i:
        return None
    done, dur = num(p["parts"][i].get("done")), num(p["parts"][i].get("duration_s"))
    return done / dur if done and dur else None


def traffic_rate(p):
    """DRAM traffic in GiB/s over the window (perf uncore_imc reads + writes)."""
    if not p:
        return None
    r, w, dur = num(p["res"].get("dram_read_gib")), num(p["res"].get("dram_write_gib")), num(p["res"].get("duration_s"))
    return (r + w) / dur if r is not None and w is not None and dur else None


def round_to(x, step, minimum):
    return max(minimum, int(round(x / step)) * step)


def memory_methods(session, base, g_idle, target, put, keep, rep):
    """Memory check: choose the vm method, then size the memory jobs with it.

    For every method and buffer size B (probe_vmm_<method>_<B>g, fixed time,
    --vm-keep): DRAM data per operation = (traffic above idle - B, the first
    touch of the buffer) / operations. A fixed number of operations is a fixed
    amount of data only if one operation moves the same data at every buffer
    size (and so also in every RQ3 split); the spread of the data per operation
    across the 4 sizes decides. 'all' is kept if its spread is at most
    MEMCHECK_KEEP_ALL_PCT points above the best method.

    (Changed 2026-10-30. The first version compared data per operation PER GB,
    assuming one operation is one pass over the buffer. The memory check showed
    that one write64 operation moves about 2 MiB at every size, so that rule
    ranked the steadiest method last and kept 'all'.)"""
    import statistics
    rep.append(f"memory check (stress-ng --vm 1 --vm-keep; idle DRAM traffic {g_idle:.3f} GiB/s subtracted, "
               f"and the first touch of the buffer):")
    planned = {r["run_id"] for r in cases.read_schedule(os.path.join(session, "schedule.csv"))}
    methods = sorted({m.group(1) for r in planned if (m := re.fullmatch(r"probe_vmm_(.+)_\dg", r))})
    data = {}                                    # method -> {gb: (GiB per op, ops/s)}
    for meth in methods:
        data[meth] = {}
        for gb in (1, 2, 4, 8):
            p = probe(session, f"probe_vmm_{meth}_{gb}g")
            s, g = part_speed(p), traffic_rate(p)
            dur = num(p["res"].get("duration_s")) if p else None
            ops = num(p["parts"][0].get("done")) if p and p["parts"] else None
            if s and g is not None and dur and ops:
                moved = (g - g_idle) * dur - gb
                if moved > 0:
                    data[meth][gb] = (moved / ops, s)
        line = ", ".join(f"{gb} GB {v[0] * 1024:.4f} MiB per op ({v[1]:.0f} ops/s)"
                         for gb, v in sorted(data[meth].items()))
        rep.append(f"  {meth}: {line or 'not measured'}")
    spread = {}
    for meth, d in data.items():
        if len(d) == 4:
            per_op = [v[0] for v in d.values()]
            spread[meth] = statistics.stdev(per_op) / statistics.mean(per_op) * 100
    for meth in methods:
        rep.append(f"  {meth}: spread of data per operation across the sizes: "
                   + (f"{spread[meth]:.1f} %" if meth in spread else "not all sizes measured"))
    if not spread:
        for gb in (1, 2, 4, 8):
            keep(f"VM_OPS_{gb}G", "no vm method measured at all four sizes")
        keep("VM_METHOD", "no vm method measured at all four sizes")
        return
    best = min(spread, key=spread.get)
    margin = float(base.get("MEMCHECK_KEEP_ALL_PCT") or 2)
    chosen = "all" if "all" in spread and spread["all"] <= spread[best] + margin else best
    why = (f"spread {spread[chosen]:.1f} %" + ("" if chosen == best else f", best single method {best} {spread[best]:.1f} %")
           + (f"; 'all' {spread['all']:.1f} % is more than {margin:g} points above the best" if "all" in spread and chosen != "all" else ""))
    put("VM_METHOD", chosen, why)
    put("VM_KEEP", 1, "the buffer is mapped once (--vm-keep)")
    per_op8, ops_s8 = data[chosen][8]
    target_gib = per_op8 * ops_s8 * target
    rep.append(f"  target: {target_gib:.1f} GiB per memory job (8 GB job with {chosen} for {target:g} s)")
    longest = 0.0
    for gb in (1, 2, 4, 8):
        per_op, ops_s = data[chosen][gb]
        ops = round_to(target_gib / per_op, 8, 8)
        longest = max(longest, ops / ops_s)
        put(f"VM_OPS_{gb}G", ops, f"{target_gib:.1f} GiB / {per_op * 1024:.4f} MiB per op, multiple of 8; about {ops / ops_s:.0f} s")
    put("VM_S", f"{longest:.1f}", "longest memory job")


def calibrate(session, target=60.0):
    base_path = os.path.join(session, "calibration.env")
    base = benchlib.read_env(base_path)
    new, rep = {}, []
    try:
        planned = {r["run_id"] for r in cases.read_schedule(os.path.join(session, "schedule.csv"))}
    except OSError:
        planned = None                      # no schedule copy: report every section

    def any_planned(prefix):
        return planned is None or any(r.startswith(prefix) for r in planned)

    def keep(key, why):
        rep.append(f"  {key} kept at {base.get(key, '?')} (not measured: {why})")

    def put(key, value, how):
        new[key] = str(value)
        rep.append(f"  {key} = {value}   ({how})")

    # -- background and idle
    idle = probe(session, "probe_idle")
    g_idle = traffic_rate(idle) or 0.0
    rep.append("background (probe_idle):")
    if idle:
        oc = num(idle["res"].get("other_cpu_per_s"))
        rep.append(f"  idle package {num(idle['res'].get('pkg_power_w')) or 0:.2f} W, DRAM {num(idle['res'].get('dram_power_w')) or 0:.2f} W, "
                   f"DRAM traffic {g_idle:.3f} GiB/s, other programs {oc if oc is not None else '?'} CPUs, "
                   f"package {num(idle['res'].get('temp_start_c')) or '?'} C")
        if oc is not None:
            put("OTHER_CPU_LIMIT", f"{min(0.5, max(0.10, round(2 * oc, 2))):.2f}", f"2 x {oc:.3f}, between 0.10 and 0.5")
        else:
            keep("OTHER_CPU_LIMIT", "no cgroup data")
    else:
        keep("OTHER_CPU_LIMIT", "probe_idle not valid")

    if planned is None or "probe_ops_semantics" in planned:
        # -- how stress-ng counts --cpu-ops
        rep.append("stress-ng operation count (probe_ops_semantics):")
        sem = probe(session, "probe_ops_semantics")
        if sem and sem["parts"] and num(sem["parts"][0].get("done")) and num(sem["parts"][0].get("flag_value")):
            p0 = sem["parts"][0]
            ratio = num(p0["done"]) / num(p0["flag_value"])
            threads = num(p0.get("threads")) or 4
            semantics = "per_instance" if ratio >= (threads + 1) / 2 else "total"
            put("OPS_SEMANTICS", semantics, f"asked {int(num(p0['flag_value']))} with {int(threads)} workers, "
                                             f"done {int(num(p0['done']))} = {ratio:.2f} x")
        else:
            keep("OPS_SEMANTICS", "probe_ops_semantics not valid")

    if any_planned("probe_cpu_"):
        # -- CPU
        rep.append("CPU (stress-ng --cpu-method all):")
        speeds = {t: part_speed(probe(session, f"probe_cpu_t{t}")) for t in (1, 2, 4, 8)}
        loads = {l: part_speed(probe(session, f"probe_cpu_load{l}")) for l in (50, 25)}
        for t, s in speeds.items():
            rep.append(f"  {t} thread(s): {s:.1f} ops/s" if s else f"  {t} thread(s): not measured")
        cpu_ops = None
        if speeds[8]:
            cpu_ops = round_to(speeds[8] * target, 800, 800)
            put("CPU_OPS", cpu_ops, f"{speeds[8]:.1f} ops/s x {target:g} s, multiple of 800")
        else:
            keep("CPU_OPS", "probe_cpu_t8 not valid")
        ops_now = cpu_ops or int(base["CPU_OPS"])
        for t, s in speeds.items():
            if s:
                put(f"CPU_S_T{t}", f"{ops_now / s:.1f}", f"{ops_now} ops / {s:.1f} ops/s")
            else:
                keep(f"CPU_S_T{t}", f"probe_cpu_t{t} not valid")
        for l, s in loads.items():
            if s:
                put(f"CPU_S_T8_LOAD{l}", f"{ops_now / s:.1f}", f"8 threads at {l} % load: {s:.1f} ops/s")
            else:
                keep(f"CPU_S_T8_LOAD{l}", f"probe_cpu_load{l} not valid")

    # -- memory: the same DRAM traffic for every buffer size
    if planned is not None and any(r.startswith("probe_vmm_") for r in planned):
        memory_methods(session, base, g_idle, target, put, keep, rep)
    elif any_planned("probe_mem_"):
        rep.append(f"memory (stress-ng --vm 1 --vm-method {base.get('VM_METHOD', 'all')}"
                   f"{' --vm-keep' if base.get('VM_KEEP') == '1' else ''}; idle DRAM traffic {g_idle:.3f} GiB/s subtracted):")
        per_op, ops_s = {}, {}
        for gb in (1, 2, 4, 8):
            p = probe(session, f"probe_mem_{gb}g")
            s, g = part_speed(p), traffic_rate(p)
            if s and g is not None and g - g_idle > 0:
                ops_s[gb], per_op[gb] = s, (g - g_idle) / s
                rep.append(f"  {gb} GB: {s:.1f} ops/s, {g - g_idle:.3f} GiB/s above idle, {per_op[gb] * 1024:.4f} MiB per operation")
            else:
                rep.append(f"  {gb} GB: not measured")
        if 8 in per_op:
            target_gib = per_op[8] * ops_s[8] * target
            rep.append(f"  target: {target_gib:.1f} GiB per memory job (8 GB job for {target:g} s)")
            longest = 0.0
            for gb in (1, 2, 4, 8):
                if gb in per_op:
                    ops = round_to(target_gib / per_op[gb], 8, 8)
                    longest = max(longest, ops / ops_s[gb])
                    put(f"VM_OPS_{gb}G", ops, f"{target_gib:.1f} GiB / {per_op[gb] * 1024:.4f} MiB per op, multiple of 8; "
                                              f"about {ops / ops_s[gb]:.0f} s")
                else:
                    keep(f"VM_OPS_{gb}G", f"probe_mem_{gb}g not valid or no DRAM traffic")
            put("VM_S", f"{longest:.1f}", "longest memory job")
        else:
            for gb in (1, 2, 4, 8):
                keep(f"VM_OPS_{gb}G", "probe_mem_8g (the target) not valid or no DRAM traffic")

    if any_planned("probe_io_"):
        # -- I/O
        rep.append("I/O (fio, 4 KiB blocks, buffered, fsync at the end):")
        seq, rnd = part_speed(probe(session, "probe_io_seq")), part_speed(probe(session, "probe_io_rand"))
        for name, s in (("sequential", seq), ("random", rnd)):
            rep.append(f"  {name}: {s / MIB:.1f} MiB/s" if s else f"  {name}: not measured")
        io_bytes = None
        if seq:
            io_bytes = max(MIB, int(seq * target // MIB) * MIB)
            put("IO_BYTES", io_bytes, f"{seq / MIB:.1f} MiB/s x {target:g} s, whole MiB ({io_bytes / 1024 ** 3:.2f} GiB)")
            put("IO_S_SEQ", f"{io_bytes / seq:.1f}", "IO_BYTES / sequential speed")
        else:
            keep("IO_BYTES", "probe_io_seq not valid")
            keep("IO_S_SEQ", "probe_io_seq not valid")
        if rnd:
            put("IO_S_RAND", f"{(io_bytes or int(base['IO_BYTES'])) / rnd:.1f}", "IO_BYTES / random speed")
        else:
            keep("IO_S_RAND", "probe_io_rand not valid")

    if planned is None or "probe_cooldown" in planned:
        # -- cooling curve
        rep.append("cooling (probe_cooldown: 8 CPU threads, then idle):")
        cool = probe(session, "probe_cooldown")
        curve = []
        if cool and cool["parts"]:
            heavy = next((p for p in cool["parts"] if p.get("kind") == "cpu"), None)
            t_end = num(heavy.get("t_end_us")) if heavy else None
            samples = benchlib.parse_sampler(benchlib.read_text(os.path.join(cool["dir"], "sampler.txt")))
            if t_end:
                curve = [((t - t_end) / 1e6, c / 1000) for t, _, _, c in samples if c is not None and t >= t_end]
        if curve:
            def at(sec):
                later = [c for s, c in curve if s >= sec]
                return later[0] if later else curve[-1][1]
            points = ", ".join(f"{s} s: {at(s):.0f} C" for s in (0, 15, 30, 60, 120, 300) if s <= curve[-1][0] or s == 0)
            rep.append(f"  package after the heavy part: {points} (last sample {curve[-1][0]:.0f} s: {curve[-1][1]:.0f} C)")
            idle_c = num(idle["res"].get("temp_start_c")) if idle else None
            limit = math.ceil(at(60))
            if idle_c is not None:
                limit = max(limit, math.ceil(idle_c) + 3)
            put("SETTLE_TEMP_C", min(75, limit), "temperature 60 s after the heavy part, rounded up"
                + (f"; at least idle {idle_c:.0f} + 3" if idle_c is not None else "") + "; at most 75")
        else:
            keep("SETTLE_TEMP_C", "probe_cooldown not valid or no temperature samples")

    # -- EPP of the default power profile (start check)
    rep.append("power setting:")
    m = re.search(r"^INFO\s+governor\s+now: governor ([^,]+), EPP (\S+)",
                  benchlib.read_text(os.path.join(session, "preflight.txt")), re.M)
    if m and m.group(2) not in ("?", "None") and "," not in m.group(2):
        put("EPP_MAIN", m.group(2), f"EPP after the start check set the power profile (governor was {m.group(1)})")
    else:
        keep("EPP_MAIN", "EPP not found in preflight.txt")

    if not any(k in new for k in ("CPU_OPS", "VM_OPS_8G", "IO_BYTES")):     # no job size measured: nothing to write
        return False, "\n".join(rep)

    # -- write the new file: the base text with the measured values replaced
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out, seen = [f"# CALIBRATED by the pilot ({stamp}) from its probe runs; see calibration_report.txt.",
                 "# Review these numbers before copying them into scripts/bench/calibration.env."], set()
    for line in benchlib.read_text(base_path).splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in new:
            out.append(f"{key}={new[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in new.items() if k not in seen]
    tmp = base_path + ".new"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    try:
        cases.load_calibration(tmp)
    except ValueError as e:
        os.remove(tmp)
        return False, "\n".join(rep) + f"\nNOT WRITTEN: the new file would not be valid: {e}"
    before = os.path.join(session, "calibration_before_pilot.env")
    if not os.path.exists(before):
        shutil.copyfile(base_path, before)
    os.replace(tmp, base_path)
    return True, "\n".join(rep)


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    session = argv[1]
    target = float(argv[2]) if len(argv) == 3 else 60.0
    try:
        ok, text = calibrate(session, target)
    except (OSError, ValueError, KeyError) as e:
        ok, text = False, f"calibration failed: {type(e).__name__}: {e}"
    head = (f"CALIBRATION {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
            f"(job size target {target:g} s)\n")
    tail = "\nRESULT: " + ("calibration.env written" if ok else "nothing written, the old values stay") + "\n"
    with open(os.path.join(session, "calibration_report.txt"), "w", encoding="utf-8") as f:
        f.write(head + text + tail)
    print(head + text + tail, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
