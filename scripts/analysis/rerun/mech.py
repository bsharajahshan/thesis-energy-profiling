#!/usr/bin/env python3

"""
mech.py - why the memory job slows down next to the CPU job (analysis piece 5)

Mechanism block, fixed time of 60 s (compare speeds and power, not energy per job):
  mech_vm_alone         memory job alone (stress-ng --vm 1, 8 GB, write64)
  mech_cpu8_vm          + 8 CPU threads (9 threads on 8 logical CPUs)
  mech_cpu7_vm          + 7 CPU threads (8 threads on 8 logical CPUs)
  mech_cpu4_vm          + 4 CPU threads
  mech_pin_own_core     memory job pinned on CPU 3, 6 CPU threads on CPUs 0,1,2,4,5,6:
                        the sibling of CPU 3 (CPU 7) stays free
  mech_pin_shared_core  memory job pinned on CPU 3, 7 CPU threads on CPUs 0,1,2,4,5,6,7:
                        one CPU thread shares the core of the memory job
cpu_t8 (8 CPU threads, no memory job, fixed work) is the reference for power and clock.
The parallel RQ2 run CPU + memory (rq2_cpu_ram_par) shows the same effect in a real
fixed-work run: its 1 s trace is split at the moment the CPU job ends.

Writes to results/rerun_2026-10-31/analysis/:
  mech_summary.csv      per case: speed of the memory job (vm ops/s, DRAM GiB/s) and of the CPU
                        job (ops/s, per thread), package power, clock, busy CPUs, IPC, temperature,
                        throttle time; memory speed relative to the job alone
  mech_tests.csv        Welch + Holm (family mech) and exact Mann-Whitney
  mech_cpu_ram_phases.csv  rq2_cpu_ram_par: package power, clock, busy CPUs and DRAM GiB/s while
                        both jobs run and after the CPU job has ended (memory job alone)
Figures (thesis/figures/):
  mech_memory.pdf       memory job speed, package power and clock per case
  mech_cpu_ram_trace.pdf  rq2_cpu_ram_par: 1 s package power and clock, end of the CPU job marked

Usage: py scripts/analysis/rerun/mech.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

CASES = ["mech_vm_alone", "mech_cpu8_vm", "mech_cpu7_vm", "mech_cpu4_vm", "mech_pin_own_core",
         "mech_pin_shared_core"]
REF_CPU = "cpu_t8"
LABEL = {"cpu_t8": "8 CPU threads, no memory job", "mech_vm_alone": "Memory job alone",
         "mech_cpu8_vm": "+ 8 CPU threads", "mech_cpu7_vm": "+ 7 CPU threads", "mech_cpu4_vm": "+ 4 CPU threads",
         "mech_pin_own_core": "Pinned, own core (+ 6 CPU)", "mech_pin_shared_core": "Pinned, shared core (+ 7 CPU)"}
CPU_THREADS = {"cpu_t8": 8, "mech_cpu8_vm": 8, "mech_cpu7_vm": 7, "mech_cpu4_vm": 4, "mech_pin_own_core": 6,
               "mech_pin_shared_core": 7}
TESTS = [
    ("memory speed: + 8 CPU vs alone", "mech_vm_alone", "mech_cpu8_vm", ["vm_ops_per_s"]),
    ("memory speed: + 7 CPU vs alone", "mech_vm_alone", "mech_cpu7_vm", ["vm_ops_per_s"]),
    ("memory speed: + 4 CPU vs alone", "mech_vm_alone", "mech_cpu4_vm", ["vm_ops_per_s"]),
    ("memory speed: own core vs alone", "mech_vm_alone", "mech_pin_own_core", ["vm_ops_per_s"]),
    ("memory speed: shared core vs alone", "mech_vm_alone", "mech_pin_shared_core", ["vm_ops_per_s"]),
    ("shared core vs own core", "mech_pin_own_core", "mech_pin_shared_core", ["vm_ops_per_s", "pkg_power_w", "busy_mhz"]),
    ("+ 8 CPU vs + 7 CPU", "mech_cpu7_vm", "mech_cpu8_vm", ["vm_ops_per_s", "pkg_power_w", "busy_mhz"]),
    ("+ 7 CPU unpinned vs pinned shared core", "mech_pin_shared_core", "mech_cpu7_vm",
     ["vm_ops_per_s", "pkg_power_w", "busy_mhz"]),
    ("+ 4 CPU unpinned vs pinned own core", "mech_pin_own_core", "mech_cpu4_vm", ["vm_ops_per_s", "pkg_power_w", "busy_mhz"]),
    ("+ 8 CPU with memory job vs 8 CPU alone", REF_CPU, "mech_cpu8_vm", ["pkg_power_w", "busy_mhz"]),
]


def per_run(runs, parts_tbl):
    r = runs[runs["case_id"].isin(CASES + [REF_CPU])].copy()
    p = parts_tbl[parts_tbl["case_id"].isin(CASES + [REF_CPU])]
    vm = p[p["kind"] == "vm"].set_index("run_id")["ops_per_s"]
    cpu = p[p["kind"] == "cpu"].set_index("run_id")["ops_per_s"]
    r["vm_ops_per_s"] = r["run_id"].map(vm)
    r["cpu_ops_per_s"] = r["run_id"].map(cpu)
    r["cpu_threads"] = r["case_id"].map(CPU_THREADS)
    r["cpu_ops_per_s_per_thread"] = r["cpu_ops_per_s"] / r["cpu_threads"]
    r["dram_gib_s"] = (r["dram_read_gib"] + r["dram_write_gib"]) / r["duration_s"]
    r["ipc"] = r["instructions"] / r["cycles"]
    r["pl1_margin_w"] = r["mmio_pl1_w_start"] - r["pkg_power_w"]
    return r


def summary(r):
    cols = ["vm_ops_per_s", "dram_gib_s", "cpu_ops_per_s", "cpu_ops_per_s_per_thread", "pkg_power_w", "pl1_margin_w",
            "pkg_power_max_1s_w", "busy_mhz", "busy_cpus", "ipc", "temp_max_c", "thr_pkg_ms", "dram_energy_j"]
    s = c.summary(r, cols)
    alone = s.set_index("case_id").loc["mech_vm_alone"]
    for col in ("vm_ops_per_s", "dram_gib_s"):
        s[f"{col}_vs_alone_pct"] = 100 * (s[f"{col}_mean"] / alone[f"{col}_mean"] - 1)
    s.insert(1, "label", s["case_id"].map(LABEL))
    order = {k: i for i, k in enumerate([REF_CPU] + CASES)}
    return s.sort_values("case_id", key=lambda k: k.map(order))


def tests(r):
    rows = []
    for name, ref, other, metrics in TESTS:
        rows += c.compare_cases(r, "mech", name, ref, other, metrics)
    return c.finish_tests(rows)


def cpu_ram_phases(runs, parts_tbl, persec):
    """rq2_cpu_ram_par: seconds while both jobs run, and after the CPU job has ended."""
    rows = []
    start = runs.set_index("run_id")["t_start_us"]
    for run_id, g in parts_tbl[parts_tbl["case_id"] == "rq2_cpu_ram_par"].groupby("run_id"):
        cpu_end = (g.loc[g["kind"] == "cpu", "t_end_us"].iloc[0] - start[run_id]) / 1e6
        k = persec[persec["run_id"] == run_id]
        both = k[(k["t_s"] > 2) & (k["t_s"] <= cpu_end)]
        after = k[k["t_s"] - k["dt"] >= cpu_end + 1]
        row = {"run_id": run_id, "cpu_end_s": cpu_end, "window_s": runs.set_index("run_id").loc[run_id, "duration_s"]}
        for name, sel in (("both", both), ("memory_only", after)):
            row[f"{name}_s"] = sel["dt"].sum()
            for col in ("pkg_w", "clock_mhz", "busy_cpus", "dram_gib_s"):
                row[f"{name}_{col}"] = sel[col].mean()
        rows.append(row)
    return pd.DataFrame(rows)


# ---- figures ------------------------------------------------------------------

def fig_memory(s, pl1):
    rows = [REF_CPU] + CASES
    t = s.set_index("case_id").loc[rows]
    y = np.arange(len(rows))[::-1]
    fig, axes = c.plt.subplots(1, 3, figsize=(c.FULL_W, 2.8), sharey=True)
    panels = (("dram_gib_s", "DRAM traffic (GiB/s)", c.JOB["ram"]), ("pkg_power_w", "Package power (W)", c.INK2),
              ("busy_mhz", "Clock while busy (MHz)", c.INK2))
    for ax, (col, label, color) in zip(axes, panels):
        ax.barh(y, t[f"{col}_mean"], height=0.62, color=color, edgecolor=c.SURFACE, linewidth=1)
        ax.errorbar(t[f"{col}_mean"], y, xerr=t[f"{col}_sd"], fmt="none", **c.ERRORBAR)
        ax.set_xlabel(label)
        ax.grid(True, axis="x")
        ax.grid(False, axis="y")
        ax.set_xlim(0, None)
    axes[1].axvline(pl1, color=c.MUTED, linestyle="--", linewidth=0.8)
    axes[1].text(pl1, len(rows) - 0.45, f"PL1 {pl1:g} W", color=c.INK2, fontsize=7, ha="center", va="bottom")
    axes[0].set_yticks(y, [LABEL[k] for k in rows])
    fig.tight_layout()
    return fig


def fig_trace(persec, phases, run_id, pl1):
    k = persec[persec["run_id"] == run_id]
    cpu_end = phases.set_index("run_id").loc[run_id, "cpu_end_s"]
    fig, axes = c.plt.subplots(1, 2, figsize=(c.FULL_W, 2.2), sharex=True)
    for ax, col, label in ((axes[0], "pkg_w", "Package power (W)"), (axes[1], "clock_mhz", "Clock while busy (MHz)")):
        ax.plot(k["t_s"], k[col], color=c.JOB["cpu"], linewidth=1.2)
        ax.axvline(cpu_end, color=c.MUTED, linestyle="--", linewidth=0.8)
        ax.set_xlabel("Time in window (s)")
        ax.set_ylabel(label)
        ax.set_ylim(0, None)
        ax.grid(True, axis="both")
    axes[0].axhline(pl1, color=c.MUTED, linestyle=":", linewidth=0.8)
    axes[0].text(cpu_end - 1, pl1 + 0.4, f"PL1 {pl1:g} W", color=c.INK2, fontsize=7, va="bottom", ha="right")
    axes[1].text(cpu_end - 1, 300, "CPU job ends", color=c.INK2, fontsize=7, ha="right")
    axes[0].set_ylim(0, 32)
    axes[1].set_ylim(0, 4500)
    fig.tight_layout()
    return fig


def main():
    runs = c.load("runs")
    parts_tbl = c.load("parts")
    r = per_run(runs, parts_tbl)
    s = summary(r)
    c.write_csv(s, "mech_summary")
    c.write_csv(tests(r), "mech_tests")

    ids = runs.loc[runs["case_id"] == "rq2_cpu_ram_par", "run_id"]
    perf = c.load("perf_1s")
    persec = c.perf_seconds(perf[perf["run_id"].isin(ids)])
    phases = cpu_ram_phases(runs, parts_tbl, persec)
    c.write_csv(phases, "mech_cpu_ram_phases")

    pl1 = runs["mmio_pl1_w_start"].iloc[0]
    # example: the run with the median package energy
    g = runs[runs["case_id"] == "rq2_cpu_ram_par"].sort_values("pkg_energy_j")
    c.setup_style()
    c.save_figure(fig_memory(s, pl1), "mech_memory")
    c.save_figure(fig_trace(persec, phases, g.iloc[len(g) // 2]["run_id"], pl1), "mech_cpu_ram_trace")


if __name__ == "__main__":
    main()
