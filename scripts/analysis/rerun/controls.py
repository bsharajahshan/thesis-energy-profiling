#!/usr/bin/env python3

"""
controls.py - the two control experiments (analysis piece 6)

1. Governor: gov_cpu_t1/2/4/8 (governor `performance`; the kernel then also sets EPP to
   `performance`) against cpu_t1/2/4/8 (Ubuntu default: governor `powersave`, EPP
   `balance_performance`). Philipp asked whether powersave flattens the curves.
2. Cost of the measurement tools: cost_*_nolog (no perf, no 1 s sampler) against the same
   job with both tools: cpu_t8, mem_8g, rq2_cpu_ram_par, idle. The bound "at most X %" is the
   larger end of the 95 % confidence interval of the difference.

Writes to results/rerun_2026-10-31/analysis/:
  controls_governor.csv  per thread count: time, package/DRAM energy, power, clock, temperature,
                         throttle time, time until 99 C, for both governors
  controls_cost.csv      per pair: package/DRAM energy and time (not for idle: fixed 60 s) with
                         and without the tools, the difference with CI, the bound, the CPU time of
                         the benchmark's own processes, and (idle pair only) the tools' own CPU use
  controls_tests.csv     Welch + Holm (family controls) and exact Mann-Whitney, for package
                         energy, DRAM energy and time
No figure: the governor runs are drawn in rq1_cpu_scaling.pdf (open markers).

Usage: py scripts/analysis/rerun/controls.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

GOV = [(1, "cpu_t1", "gov_cpu_t1"), (2, "cpu_t2", "gov_cpu_t2"), (4, "cpu_t4", "gov_cpu_t4"), (8, "cpu_t8", "gov_cpu_t8")]
COST = [("cpu_t8", "cost_cpu_t8_nolog"), ("mem_8g", "cost_mem_8g_nolog"),
        ("rq2_cpu_ram_par", "cost_cpu_ram_par_nolog"), ("idle", "cost_idle_nolog")]
METRICS = ["pkg_energy_j", "dram_energy_j", "duration_s"]
COST_NOTE = {"idle": "logged idle runs at the start and end of the session (start 46.7 C), "
                     "unlogged idle runs inside the rounds (start 49 C)"}
FIXED_TIME = {"idle"}    # time is set (60 s), not a result: not tested
# the tools' own CPU use is only clean in the idle pair; in the other pairs the job's own CPU
# time also changes with the run length


def metrics_for(case):
    return [m for m in METRICS if not (m == "duration_s" and case in FIXED_TIME)]


def governor(runs):
    phases = pd.read_csv(c.OUT / "rq1_trace_phases.csv")   # written by rq1.py
    t99 = phases.groupby("case_id")["t_to_99c_s"].mean()
    rows = []
    for threads, ps, perf in GOV:
        for gov, case in (("powersave", ps), ("performance", perf)):
            g = runs[runs["case_id"] == case]
            rows.append({"threads": threads, "governor": gov, "case_id": case, "epp": g["epp_start"].iloc[0],
                         "n": len(g), "duration_s": g["duration_s"].mean(), "pkg_energy_j": g["pkg_energy_j"].mean(),
                         "pkg_energy_j_sd": g["pkg_energy_j"].std(ddof=1), "dram_energy_j": g["dram_energy_j"].mean(),
                         "pkg_power_w": g["pkg_power_w"].mean(), "busy_mhz": g["busy_mhz"].mean(),
                         "temp_max_c": g["temp_max_c"].mean(), "thr_pkg_ms": g["thr_pkg_ms"].mean(),
                         "t_to_99c_s": t99.get(case)})
    return pd.DataFrame(rows)


def cost(runs):
    rows = []
    for logged, nolog in COST:
        a, b = runs[runs["case_id"] == logged], runs[runs["case_id"] == nolog]
        for m in metrics_for(logged):
            w = c.welch(a[m], b[m])
            bound = max(abs(w["diff_pct_lo"]), abs(w["diff_pct_hi"])) if pd.notna(w["diff_pct_lo"]) else float("nan")
            rows.append({"logged": logged, "nolog": nolog, "metric": m, "n_logged": len(a), "n_nolog": len(b),
                         "mean_logged": w["mean_ref"], "mean_nolog": w["mean_other"], "diff_pct": w["diff_pct"],
                         "diff_pct_lo": w["diff_pct_lo"], "diff_pct_hi": w["diff_pct_hi"], "bound_pct": bound,
                         "cpu_ours_s_logged": a["cpu_ours_s"].mean(), "cpu_ours_s_nolog": b["cpu_ours_s"].mean(),
                         "tool_cpu_per_s": ((a["cpu_ours_s"].mean() - b["cpu_ours_s"].mean()) / a["duration_s"].mean()
                                            if logged == "idle" else float("nan")),
                         "temp_start_logged": a["temp_start_c"].mean(), "temp_start_nolog": b["temp_start_c"].mean(),
                         "note": COST_NOTE.get(logged, "")})
    return pd.DataFrame(rows)


def tests(runs):
    rows = []
    for threads, ps, perf in GOV:
        rows += c.compare_cases(runs, "controls", f"governor performance vs powersave, {threads} thr", ps, perf, METRICS)
    for logged, nolog in COST:
        rows += c.compare_cases(runs, "controls", f"without tools vs with tools, {logged}", logged, nolog,
                                metrics_for(logged))
    return c.finish_tests(rows)


def main():
    runs = c.load("runs")
    c.write_csv(governor(runs), "controls_governor")
    c.write_csv(cost(runs), "controls_cost")
    c.write_csv(tests(runs), "controls_tests")


if __name__ == "__main__":
    main()
