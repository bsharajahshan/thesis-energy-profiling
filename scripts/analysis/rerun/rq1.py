#!/usr/bin/env python3

"""
rq1.py - RQ1: every job alone at four levels (analysis piece 2)

Cases: cpu_t1/2/4/8 (412000 bogo-ops), mem_1g/2g/4g/8g (the same DRAM data),
io_seq/io_rand (6.62 GB written and flushed). The governor control runs
(gov_cpu_t*) are only drawn next to the CPU runs here; their tests are in
controls.py.

Writes to results/rerun_2026-10-31/analysis/:
  rq1_summary.csv       per case: mean, SD, CV of time, package/DRAM energy and power,
                        energy above idle and idle part, clock, busy CPUs, IPC, energy per
                        unit of work, work per second, work per joule (package), EDP
  rq1_cpu_scaling.csv   CPU jobs against 1 thread: speed-up = busy CPUs x clock x IPC
                        (x instructions ratio), power ratio, energy ratio
  rq1_clock_energy_fit.csv  CPU-only fixed-work runs (RQ1, governor, RQ3 CPU): energy above
                        idle against clock, relative to the case mean (same thread count and
                        work, only the clock differs)
  rq1_trace_phases.csv  per CPU run: time until 99 C, power and clock in the first 10 s and
                        after 30 s (from the 1 s samples and perf)
  rq1_traces.csv        the 1 s values of the three runs in the trace figure
  rq1_io_fio.csv        per I/O run: fio write bandwidth, runtime and latency from part_0.json
  rq1_tests.csv         Welch + Holm (family rq1) and exact Mann-Whitney: neighbouring
                        levels of each job, for package energy, DRAM energy and time
Figures (thesis/figures/):
  rq1_energy.pdf        package and DRAM energy per job, idle part marked
  rq1_cpu_scaling.pdf   time, package power and clock against threads (governor runs open)
  rq1_traces.pdf        1 s package power, temperature and clock of a 1-, 2- and 8-thread run

Usage: py scripts/analysis/rerun/rq1.py
"""

import json
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

CPU = ["cpu_t1", "cpu_t2", "cpu_t4", "cpu_t8"]
GOV = ["gov_cpu_t1", "gov_cpu_t2", "gov_cpu_t4", "gov_cpu_t8"]
MEM = ["mem_1g", "mem_2g", "mem_4g", "mem_8g"]
IO = ["io_seq", "io_rand"]
THREADS = [1, 2, 4, 8]
JOB_OF = {**{k: "cpu" for k in CPU + GOV}, **{k: "ram" for k in MEM}, **{k: "io" for k in IO}}
UNIT = {"cpu": "1000 bogo-ops", "ram": "GiB DRAM traffic", "io": "GB written"}

TESTS = [("cpu 1 vs 2 threads", "cpu_t1", "cpu_t2"), ("cpu 2 vs 4 threads", "cpu_t2", "cpu_t4"),
         ("cpu 4 vs 8 threads", "cpu_t4", "cpu_t8"),
         ("memory 1 vs 2 GB", "mem_1g", "mem_2g"), ("memory 2 vs 4 GB", "mem_2g", "mem_4g"),
         ("memory 4 vs 8 GB", "mem_4g", "mem_8g"),
         ("io sequential vs random", "io_seq", "io_rand")]
METRICS = ["pkg_energy_j", "dram_energy_j", "duration_s"]

TJMAX_C = 100            # i7-8550U junction temperature limit
TRACE_CASES = ["cpu_t1", "cpu_t2", "cpu_t8"]


def per_run(runs, idle):
    r = c.add_active(runs[runs["case_id"].isin(JOB_OF)], idle)
    r["job"] = r["case_id"].map(JOB_OF)
    r["dram_gib"] = r["dram_read_gib"] + r["dram_write_gib"]
    r["ipc"] = r["instructions"] / r["cycles"]
    r["nj_per_instruction"] = 1e9 * r["pkg_energy_j"] / r["instructions"]
    r["edp_js"] = r["pkg_energy_j"] * r["duration_s"]
    work = {"cpu": r["cpu_ops_done"] / 1000, "ram": r["dram_gib"], "io": r["io_bytes_done"] / 1e9}
    r["work"] = np.select([r["job"] == j for j in work], [work[j] for j in work])
    for col in ("pkg_energy_j", "pkg_active_j", "dram_energy_j", "dram_active_j"):
        r[col.replace("_j", "_per_work")] = r[col] / r["work"]
    r["work_per_s"] = r["work"] / r["duration_s"]
    r["work_per_pkg_j"] = r["work"] / r["pkg_energy_j"]
    return r


SUMMARY_COLS = ["duration_s", "pkg_energy_j", "dram_energy_j", "pkg_power_w", "dram_power_w",
                "pkg_active_j", "pkg_idle_j", "dram_active_j", "dram_idle_j", "busy_mhz", "busy_cpus",
                "ipc", "instructions", "nj_per_instruction", "dram_gib", "work", "pkg_energy_per_work",
                "pkg_active_per_work", "dram_energy_per_work", "dram_active_per_work", "work_per_s",
                "work_per_pkg_j", "edp_js", "temp_max_c", "thr_pkg_ms"]


def summary(r):
    s = c.summary(r, SUMMARY_COLS)
    s.insert(1, "job", s["case_id"].map(JOB_OF))
    s.insert(2, "work_unit", s["job"].map(UNIT))
    order = {k: i for i, k in enumerate(CPU + GOV + MEM + IO)}
    return s.sort_values("case_id", key=lambda k: k.map(order))


def cpu_scaling(s):
    s = s.set_index("case_id")
    base = s.loc["cpu_t1"]
    rows = []
    for case, t in zip(CPU, THREADS):
        x = s.loc[case]
        speedup = base["duration_s_mean"] / x["duration_s_mean"]
        busy = x["busy_cpus_mean"] / base["busy_cpus_mean"]
        clock = x["busy_mhz_mean"] / base["busy_mhz_mean"]
        ipc = x["ipc_mean"] / base["ipc_mean"]
        instr = base["instructions_mean"] / x["instructions_mean"]
        power = x["pkg_power_w_mean"] / base["pkg_power_w_mean"]
        rows.append({"case_id": case, "threads": t, "speedup": speedup, "busy_cpus_ratio": busy,
                     "clock_ratio": clock, "ipc_ratio": ipc, "instructions_ratio": instr,
                     "product": busy * clock * ipc * instr, "pkg_power_ratio": power,
                     "pkg_energy_ratio": x["pkg_energy_j_mean"] / base["pkg_energy_j_mean"],
                     "pkg_active_ratio": x["pkg_active_j_mean"] / base["pkg_active_j_mean"],
                     "idle_part_share_pct": 100 * x["pkg_idle_j_mean"] / x["pkg_energy_j_mean"]})
    return pd.DataFrame(rows)


def clock_energy_fit(runs, idle):
    """Within a case (same threads, same work) only the clock differs between runs."""
    only_cpu = runs["parts"].str.split("+").apply(lambda p: set(p) == {"cpu"})
    sel = c.add_active(runs[(runs["size"] == "fixed work") & only_cpu & runs["busy_mhz"].notna()], idle)
    rows = []
    for col in ("pkg_active_j", "pkg_energy_j", "duration_s"):
        x = 100 * (sel["busy_mhz"] / sel.groupby("case_id")["busy_mhz"].transform("mean") - 1)
        y = 100 * (sel[col] / sel.groupby("case_id")[col].transform("mean") - 1)
        lr = sp_stats.linregress(x, y)
        half = sp_stats.t.ppf(0.975, len(x) - 2) * lr.stderr
        rows.append({"y": f"{col} % from case mean", "x": "busy_mhz % from case mean", "slope": lr.slope,
                     "slope_lo": lr.slope - half, "slope_hi": lr.slope + half, "r": lr.rvalue,
                     "n_runs": len(x), "n_cases": sel["case_id"].nunique()})
    return pd.DataFrame(rows)


# ---- 1 s traces ---------------------------------------------------------------

def trace_phases(runs, samples, clock):
    rows = []
    for _, run in runs[runs["case_id"].isin(CPU + GOV)].iterrows():
        s = samples[(samples["run_id"] == run["run_id"]) & (samples["in_window"] == 1)]
        k = clock[clock["run_id"] == run["run_id"]]
        hot = s.loc[s["temp_c"] >= TJMAX_C - 1, "t_s"]
        rows.append({"run_id": run["run_id"], "case_id": run["case_id"], "duration_s": run["duration_s"],
                     "t_to_99c_s": hot.min() if len(hot) else np.nan,
                     "pkg_w_first10s": s.loc[s["t_s"] <= 10, "pkg_w"].mean(),
                     "pkg_w_after30s": s.loc[s["t_s"] > 30, "pkg_w"].mean(),
                     "temp_c_after30s": s.loc[s["t_s"] > 30, "temp_c"].mean(),
                     "clock_first10s": k.loc[k["t_s"] <= 10, "clock_mhz"].mean(),
                     "clock_after30s": k.loc[k["t_s"] > 30, "clock_mhz"].mean(),
                     "thr_pkg_ms": run["thr_pkg_ms"]})
    return pd.DataFrame(rows)


def median_run(runs, case):
    """The run whose package energy is the median of its case (5 runs)."""
    g = runs[runs["case_id"] == case].sort_values("pkg_energy_j")
    return g.iloc[len(g) // 2]["run_id"]


def traces(runs, samples, clock):
    out = []
    for case in TRACE_CASES:
        rid = median_run(runs, case)
        s = samples[(samples["run_id"] == rid) & (samples["in_window"] == 1)][["t_s", "pkg_w", "temp_c"]]
        k = clock[clock["run_id"] == rid][["t_s", "clock_mhz", "busy_cpus"]]
        # perf seconds count from perf's start, sampler seconds from the window start;
        # both are about 1 s apart, so they are matched to the nearest second
        s = s.assign(sec=s["t_s"].round().astype(int))
        k = k.assign(sec=k["t_s"].round().astype(int))
        m = s.merge(k.drop(columns="t_s"), on="sec", how="outer").sort_values("sec")
        m.insert(0, "case_id", case)
        m.insert(0, "run_id", rid)
        out.append(m)
    return pd.concat(out, ignore_index=True)


# ---- I/O --------------------------------------------------------------------

def io_fio(runs):
    rows = []
    for _, run in runs[runs["case_id"].isin(IO)].iterrows():
        path = c.DATA / "raw" / "main" / "main" / "runs" / run["run_id"] / f"a{int(run['attempt'])}" / "part_0.json"
        with open(path, encoding="utf-8") as f:
            job = json.load(f)["jobs"][0]
        w = job["write"]
        rows.append({"run_id": run["run_id"], "case_id": run["case_id"], "window_s": run["duration_s"],
                     "fio_runtime_s": w["runtime"] / 1000, "bw_mib_s": w["bw"] / 1024,
                     "clat_mean_us": w["clat_ns"]["mean"] / 1000, "iops": w["iops"],
                     "busy_cpus": run["busy_cpus"], "context_switches": run["context_switches"],
                     "disk_written_per_fio_bytes": run["disk_written_bytes"] / run["io_bytes_done"]})
    return pd.DataFrame(rows)


# ---- figures ------------------------------------------------------------------

def stacked(ax, x, total, idle_part, sd, color):
    """Bar = idle part (hatched gray) + energy above idle (job colour), error bar on the total.
    A job at the idle level (energy above idle <= 0) is drawn as idle part only."""
    active = np.clip(total - idle_part, 0, None)
    base = total - active
    ax.bar(x, base, width=0.7, color=c.GRID, hatch=c.IDLE_HATCH, edgecolor=c.SURFACE, linewidth=1,
           hatchcolor=c.MUTED)
    ax.bar(x, active, bottom=base, width=0.7, color=color, edgecolor=c.SURFACE, linewidth=1)
    ax.errorbar(x, total, yerr=sd, fmt="none", **c.ERRORBAR)


def fig_energy(s):
    s = s.set_index("case_id")
    groups = [(CPU, ["1", "2", "4", "8"], "Threads", "cpu"),
              (MEM, ["1", "2", "4", "8"], "Buffer size (GB)", "ram"),
              (IO, ["seq.", "random"], "Write pattern", "io")]
    fig, axes = c.plt.subplots(2, 3, figsize=(c.FULL_W, 4.4), gridspec_kw={"width_ratios": [4, 4, 2.3]})
    for col, (cases, labels, xlabel, job) in enumerate(groups):
        x = np.arange(len(cases))
        for row, dom in enumerate(("pkg", "dram")):
            ax = axes[row, col]
            stacked(ax, x, s.loc[cases, f"{dom}_energy_j_mean"].to_numpy(),
                    s.loc[cases, f"{dom}_idle_j_mean"].to_numpy(),
                    s.loc[cases, f"{dom}_energy_j_sd"].to_numpy(), c.JOB[job])
            ax.set_xticks(x, labels)
            ax.set_xlim(-0.6, len(cases) - 0.4)
            if row == 1:
                ax.set_xlabel(xlabel)
    axes[0, 0].set_ylabel("Package energy (J)")
    axes[1, 0].set_ylabel("DRAM energy (J)")
    handles = [c.plt.Rectangle((0, 0), 1, 1, color=c.JOB["cpu"]), c.plt.Rectangle((0, 0), 1, 1, color=c.JOB["ram"]),
               c.plt.Rectangle((0, 0), 1, 1, color=c.JOB["io"]),
               c.plt.Rectangle((0, 0), 1, 1, facecolor=c.GRID, hatch=c.IDLE_HATCH, edgecolor=c.MUTED, linewidth=0)]
    fig.legend(handles, ["CPU job", "Memory job", "I/O job", "Idle part (idle power × time)"],
               loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.04), handlelength=1.4)
    fig.tight_layout()
    return fig


def fig_cpu_scaling(s, pl):
    s = s.set_index("case_id")
    x = np.arange(len(THREADS))
    panels = [("duration_s", "Time per job (s)"), ("pkg_power_w", "Package power (W)"),
              ("busy_mhz", "Clock while busy (MHz)")]
    fig, axes = c.plt.subplots(1, 3, figsize=(c.FULL_W, 2.3))
    for ax, (col, label) in zip(axes, panels):
        for cases, dx, face, name in ((CPU, -0.06, c.JOB["cpu"], "powersave (default)"),
                                      (GOV, 0.06, c.SURFACE, "performance")):
            ax.errorbar(x + dx, s.loc[cases, f"{col}_mean"], yerr=s.loc[cases, f"{col}_sd"],
                        color=c.JOB["cpu"], marker="o", markerfacecolor=face, markeredgecolor=c.JOB["cpu"],
                        linestyle="-" if face != c.SURFACE else "none", label=f"Governor {name}", **c.ERRORBAR)
        ax.set_xticks(x, [str(t) for t in THREADS])
        ax.set_xlabel("Threads")
        ax.set_ylabel(label)
    for value, name, va, dy in ((pl["pl1"], "PL1", "top", -0.4), (pl["pl2"], "PL2", "bottom", 0.4)):
        axes[1].axhline(value, color=c.MUTED, linestyle="--", linewidth=0.8, zorder=1)
        axes[1].text(-0.3, value + dy, f"{name} {value:g} W", color=c.INK2, fontsize=7, va=va)
    axes[1].set_ylim(0, 33)
    axes[0].set_ylim(0, None)
    axes[2].set_ylim(0, 4500)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.08))
    fig.tight_layout()
    return fig


def fig_traces(tr, pl):
    fig, axes = c.plt.subplots(3, len(TRACE_CASES), figsize=(c.FULL_W, 4.8), sharey="row")
    for j, case in enumerate(TRACE_CASES):
        t = tr[tr["case_id"] == case]
        for i, (col, label) in enumerate((("pkg_w", "Package power (W)"), ("temp_c", "Temperature (°C)"),
                                          ("clock_mhz", "Clock while busy (MHz)"))):
            ax = axes[i, j]
            ax.plot(t["sec"], t[col], color=c.JOB["cpu"], linewidth=1.2)
            ax.grid(True, axis="both")
            if j == 0:
                ax.set_ylabel(label)
            if i == 2:
                ax.set_xlabel("Time in window (s)")
        threads = int(case.split("_t")[1])
        axes[0, j].text(0.02, 0.04, f"{threads} thread{'s' if threads > 1 else ''}", transform=axes[0, j].transAxes,
                        fontsize=8, color=c.INK)
        for value in (pl["pl1"], pl["pl2"]):
            axes[0, j].axhline(value, color=c.MUTED, linestyle="--", linewidth=0.8, zorder=1)
        axes[1, j].axhline(TJMAX_C, color=c.MUTED, linestyle="--", linewidth=0.8, zorder=1)
    axes[0, 0].set_ylim(0, 32)
    axes[1, 0].set_ylim(40, 104)
    axes[2, 0].set_ylim(0, 4500)
    fig.tight_layout()
    return fig


def main():
    runs = c.load("runs")
    idle = c.idle_reference(runs)
    r = per_run(runs, idle)
    s = summary(r)
    c.write_csv(s, "rq1_summary")
    c.write_csv(cpu_scaling(s), "rq1_cpu_scaling")
    c.write_csv(clock_energy_fit(runs, idle), "rq1_clock_energy_fit")

    ids = runs.loc[runs["case_id"].isin(CPU + GOV), "run_id"]
    samples = c.load("samples_1s")
    samples = samples[samples["run_id"].isin(ids)]
    perf = c.load("perf_1s")
    clock = c.perf_seconds(perf[perf["run_id"].isin(ids)])
    c.write_csv(trace_phases(runs, samples, clock), "rq1_trace_phases")
    tr = traces(runs, samples, clock)
    c.write_csv(tr, "rq1_traces")
    c.write_csv(io_fio(runs), "rq1_io_fio")

    rows = []
    for name, ref, other in TESTS:
        rows += c.compare_cases(runs, "rq1", name, ref, other, METRICS)
    c.write_csv(c.finish_tests(rows), "rq1_tests")

    pl = {"pl1": runs["mmio_pl1_w_start"].iloc[0], "pl2": runs["mmio_pl2_w_start"].iloc[0]}
    c.setup_style()
    c.save_figure(fig_energy(s), "rq1_energy")
    c.save_figure(fig_cpu_scaling(s, pl), "rq1_cpu_scaling")
    c.save_figure(fig_traces(tr, pl), "rq1_traces")


if __name__ == "__main__":
    main()
