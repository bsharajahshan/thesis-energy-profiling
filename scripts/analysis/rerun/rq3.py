#!/usr/bin/env python3

"""
rq3.py - RQ3: the same work as one task or as N smaller tasks (analysis piece 4)

Series (N = 1, 2, 4, 8; every part has 1/N of the work):
  CPU parallel / sequential   parts with 8/N threads; N=1 is cpu_t8
  RAM parallel / sequential   parts with an 8/N GB buffer; N=1 is mem_8g
  CPU 50 % and 25 % load      parallel only; N=1 is its own case (rq3_cpu_load*_n1)
The CPU parallel series is the 100 % load series.

Writes to results/rerun_2026-10-31/analysis/:
  rq3_summary.csv     per series and N: mean, SD, CV of package/DRAM energy (total, above idle),
                      time, power, clock, busy CPUs, IPC, DRAM traffic, energy per instruction
  rq3_tests.csv       Welch + Holm (family rq3) and exact Mann-Whitney, for package energy,
                      DRAM energy and time: every N against N=1 of its series; sequential N
                      against the unsplit job with the same threads / buffer (cost of
                      splitting); 50 % and 25 % load against 100 % load (N=1)
  rq3_parts.csv       every part of the sequential runs: position, time, own energy, kernel time,
                      against 1/N of the unsplit job with the same threads / buffer
  rq3_parts_summary.csv  the same per case and position
  rq3_ram.csv         RAM series: busy CPUs, DRAM GiB/s, package power, clock, IPC, DRAM energy
                      per GiB and its error against the DRAM model, kernel time
  rq3_low_clock.csv   partial-load runs: seconds at a low clock (< 1.5 GHz), time, energy
  rq3_low_clock_fit.csv  time and energy against the low-clock seconds, per load level
Figures (thesis/figures/):
  rq3_split.pdf       package energy, DRAM energy and time against N, CPU and RAM
  rq3_partial_load.pdf  package energy, time and clock against N at 100 / 50 / 25 % load
  rq3_low_clock.pdf   50 % load: clock per second of a fast and a slow run, and time and energy
                      against the seconds at low clock (all 20 runs)

Usage: py scripts/analysis/rerun/rq3.py
"""

import os
import sys

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

NS = [1, 2, 4, 8]
SERIES = {
    "cpu_par": ["cpu_t8"] + [f"rq3_cpu_par_n{n}" for n in NS[1:]],
    "cpu_seq": ["cpu_t8"] + [f"rq3_cpu_seq_n{n}" for n in NS[1:]],
    "ram_par": ["mem_8g"] + [f"rq3_ram_par_n{n}" for n in NS[1:]],
    "ram_seq": ["mem_8g"] + [f"rq3_ram_seq_n{n}" for n in NS[1:]],
    "load50": [f"rq3_cpu_load50_n{n}" for n in NS],
    "load25": [f"rq3_cpu_load25_n{n}" for n in NS],
}
# the unsplit job that does the whole work with the threads / buffer of one sequential part
UNSPLIT = {"rq3_cpu_seq_n2": "cpu_t4", "rq3_cpu_seq_n4": "cpu_t2", "rq3_cpu_seq_n8": "cpu_t1",
           "rq3_ram_seq_n2": "mem_4g", "rq3_ram_seq_n4": "mem_2g", "rq3_ram_seq_n8": "mem_1g"}
METRICS = ["pkg_energy_j", "dram_energy_j", "duration_s"]
LOW_CLOCK_MHZ = 1500
SERIES_NAME = {"cpu_par": "CPU, parallel", "cpu_seq": "CPU, sequential", "ram_par": "RAM, parallel",
               "ram_seq": "RAM, sequential", "load50": "CPU 50 % load", "load25": "CPU 25 % load"}


def per_run(runs, idle):
    r = c.add_active(runs, idle)
    r["dram_gib"] = r["dram_read_gib"] + r["dram_write_gib"]
    r["dram_gib_s"] = r["dram_gib"] / r["duration_s"]
    r["dram_j_per_gib"] = r["dram_energy_j"] / r["dram_gib"]
    r["ipc"] = r["instructions"] / r["cycles"]
    r["nj_per_instruction"] = 1e9 * r["pkg_energy_j"] / r["instructions"]
    return r


SUMMARY_COLS = ["pkg_energy_j", "pkg_active_j", "pkg_idle_j", "dram_energy_j", "dram_active_j", "duration_s",
                "pkg_power_w", "busy_mhz", "busy_cpus", "ipc", "dram_gib", "dram_gib_s", "dram_j_per_gib",
                "nj_per_instruction", "thr_pkg_ms", "temp_max_c"]


def summary(r):
    out = []
    for name, cases in SERIES.items():
        s = c.summary(r[r["case_id"].isin(cases)], SUMMARY_COLS)
        s.insert(0, "series", name)
        s.insert(1, "n_parts", s["case_id"].map({k: n for k, n in zip(cases, NS)}))
        out.append(s.sort_values("n_parts"))
    return pd.concat(out, ignore_index=True)


def tests(runs):
    rows = []
    for name, cases in SERIES.items():
        for case, n in zip(cases[1:], NS[1:]):
            rows += c.compare_cases(runs, "rq3", f"{name}: N={n} vs N=1", cases[0], case, METRICS)
    for case, ref in UNSPLIT.items():
        rows += c.compare_cases(runs, "rq3", f"split cost: {case} vs {ref}", ref, case, METRICS)
    for load in ("load50", "load25"):
        rows += c.compare_cases(runs, "rq3", f"{load} vs 100 % (N=1)", "cpu_t8", SERIES[load][0], METRICS)
    return c.finish_tests(rows)


def seq_parts(runs, parts_tbl):
    ref = runs.groupby("case_id")[["pkg_energy_j", "dram_energy_j", "duration_s"]].mean()
    sel = parts_tbl[parts_tbl["case_id"].isin(UNSPLIT)]
    rows = []
    for run_id, g in sel.groupby("run_id"):
        case = g["case_id"].iloc[0]
        n = len(g)
        u = ref.loc[UNSPLIT[case]]
        for pos, (_, p) in enumerate(g.sort_values("t_start_us").iterrows(), start=1):
            rows.append({"run_id": run_id, "case_id": case, "n_parts": n, "position": pos,
                         "duration_s": p["duration_s"], "pkg_energy_j": p["sub_pkg_energy_j"],
                         "dram_energy_j": p["sub_dram_energy_j"], "sys_s": p["sys_s"],
                         "pkg_ratio": p["sub_pkg_energy_j"] / (u["pkg_energy_j"] / n),
                         "duration_ratio": p["duration_s"] / (u["duration_s"] / n)})
    pt = pd.DataFrame(rows)
    g = pt.groupby(["case_id", "position"])
    ps = pd.DataFrame({"n": g.size()})
    for col in ("duration_s", "pkg_energy_j", "pkg_ratio", "duration_ratio", "sys_s"):
        ps[f"{col}_mean"] = g[col].mean()
        ps[f"{col}_sd"] = g[col].std(ddof=1)
    return pt, ps.reset_index()


def ram_table(r, parts_tbl, dm):
    cases = sorted(set(SERIES["ram_par"] + SERIES["ram_seq"]))
    x = r[r["case_id"].isin(cases)]
    g = x.groupby("case_id")
    out = pd.DataFrame({
        "n": g.size(), "busy_cpus": g["busy_cpus"].mean(), "dram_gib": g["dram_gib"].mean(),
        "dram_gib_s": g["dram_gib_s"].mean(), "pkg_power_w": g["pkg_power_w"].mean(),
        "busy_mhz": g["busy_mhz"].mean(), "ipc": g["ipc"].mean(), "dram_j_per_gib": g["dram_j_per_gib"].mean(),
        "duration_s": g["duration_s"].mean(), "pkg_energy_j": g["pkg_energy_j"].mean(),
    })
    out["sys_s_total"] = parts_tbl[parts_tbl["case_id"].isin(cases)].groupby("case_id")["sys_s"].sum() / out["n"]
    out = out.join(dm.set_index("case_id")["err_pct_mean"].rename("dram_model_err_pct"))
    out["n_parts"] = [1 if k == "mem_8g" else int(k.rsplit("_n", 1)[1]) for k in out.index]
    out["mode"] = ["-" if k == "mem_8g" else ("parallel" if "_par_" in k else "sequential") for k in out.index]
    return out.reset_index().sort_values(["mode", "n_parts"])


def low_clock(runs, persec):
    cases = SERIES["load50"] + SERIES["load25"]
    x = persec.merge(runs[["run_id", "case_id", "duration_s", "pkg_energy_j", "rep"]], on="run_id")
    x = x[x["case_id"].isin(cases)]
    x["low"] = x["clock_mhz"] < LOW_CLOCK_MHZ
    g = x.groupby("run_id")
    out = pd.DataFrame({
        "case_id": g["case_id"].first(), "rep": g["rep"].first(),
        "low_clock_s": g.apply(lambda d: (d["dt"] * d["low"]).sum()),
        "first_high_s": g.apply(lambda d: d.loc[d["clock_mhz"] >= 2000, "t_s"].min()),
        "clock_high_mhz": g.apply(lambda d: d.loc[~d["low"] & d["clock_mhz"].notna(), "clock_mhz"].mean()),
        "clock_low_mhz": g.apply(lambda d: d.loc[d["low"], "clock_mhz"].mean()),
        "pkg_w_low": g.apply(lambda d: d.loc[d["low"], "pkg_w"].mean()),
        "pkg_w_high": g.apply(lambda d: d.loc[~d["low"], "pkg_w"].mean()),
        "duration_s": g["duration_s"].first(), "pkg_energy_j": g["pkg_energy_j"].first(),
    }).reset_index()
    out["load"] = out["case_id"].str.extract(r"load(\d+)")[0].astype(int)
    out["n_parts"] = out["case_id"].str.extract(r"_n(\d+)")[0].astype(int)
    fits = []
    for load, d in out.groupby("load"):
        for y in ("duration_s", "pkg_energy_j"):
            lr = sp_stats.linregress(d["low_clock_s"], d[y])
            half = sp_stats.t.ppf(0.975, len(d) - 2) * lr.stderr
            fits.append({"load": load, "y": y, "x": "low_clock_s", "intercept": lr.intercept, "slope": lr.slope,
                         "slope_lo": lr.slope - half, "slope_hi": lr.slope + half, "r": lr.rvalue, "n": len(d),
                         "low_s_min": d["low_clock_s"].min(), "low_s_max": d["low_clock_s"].max()})
    return out.sort_values(["load", "n_parts", "rep"]), pd.DataFrame(fits)


# ---- figures ------------------------------------------------------------------

def line(ax, s, series, col, color, mode, label):
    t = s[s["series"] == series]
    filled = mode == "parallel"
    ax.errorbar(range(len(NS)), t[f"{col}_mean"], yerr=t[f"{col}_sd"], color=color, marker="o",
                markerfacecolor=color if filled else c.SURFACE, markeredgecolor=color,
                linestyle=c.MODE_LINE[mode], label=label, **c.ERRORBAR)


def fig_split(s):
    rows = (("pkg_energy_j", "Package energy (J)"), ("dram_energy_j", "DRAM energy (J)"), ("duration_s", "Time (s)"))
    fig, axes = c.plt.subplots(3, 2, figsize=(c.FULL_W, 5.6), sharex=True)
    for j, (job, name) in enumerate((("cpu", "CPU job"), ("ram", "Memory job"))):
        for i, (col, label) in enumerate(rows):
            ax = axes[i, j]
            for mode in ("parallel", "sequential"):
                line(ax, s, f"{job}_{mode[:3]}", col, c.JOB[job], mode, f"{name}, {mode}")
            ax.set_ylim(0, None)
            if j == 0:
                ax.set_ylabel(label)
        axes[2, j].set_xticks(range(len(NS)), [str(n) for n in NS])
        axes[2, j].set_xlabel("Number of parts N")
        axes[0, j].legend(loc="upper left", fontsize=7)
    fig.tight_layout()
    return fig


def fig_partial_load(s):
    panels = (("pkg_energy_j", "Package energy (J)"), ("duration_s", "Time (s)"), ("busy_mhz", "Clock while busy (MHz)"))
    fig, axes = c.plt.subplots(1, 3, figsize=(c.FULL_W, 2.4))
    for ax, (col, label) in zip(axes, panels):
        for load, series in ((100, "cpu_par"), (50, "load50"), (25, "load25")):
            t = s[s["series"] == series]
            ax.errorbar(range(len(NS)), t[f"{col}_mean"], yerr=t[f"{col}_sd"], color=c.LOAD[load], marker="o",
                        label=f"{load} % load", **c.ERRORBAR)
        ax.set_xticks(range(len(NS)), [str(n) for n in NS])
        ax.set_xlabel("Number of parts N")
        ax.set_ylabel(label)
        ax.set_ylim(0, None)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.08))
    fig.tight_layout()
    return fig


def fig_low_clock(low, fits, persec, fast, slow):
    fig, axes = c.plt.subplots(1, 3, figsize=(c.FULL_W, 2.4), gridspec_kw={"width_ratios": [1.4, 1, 1]})
    ax = axes[0]
    for rid, style, name in ((fast, "-", "fast run"), (slow, "--", "slow run")):
        k = persec[persec["run_id"] == rid]
        ax.plot(k["t_s"], k["clock_mhz"], color=c.JOB["cpu"], linestyle=style, linewidth=1.1,
                label=f"{name} ({low.set_index('run_id').loc[rid, 'duration_s']:.0f} s)")
    ax.set_xlabel("Time in window (s)")
    ax.set_ylabel("Clock while busy (MHz)")
    ax.set_ylim(0, 4200)
    ax.legend(loc="lower right", fontsize=7)
    d = low[low["load"] == 50]
    for ax, y, label in ((axes[1], "duration_s", "Time (s)"), (axes[2], "pkg_energy_j", "Package energy (J)")):
        for n in NS:
            e = d[d["n_parts"] == n]
            ax.scatter(e["low_clock_s"], e[y], s=16, color=c.THREADS[n], edgecolor=c.SURFACE, linewidth=0.5,
                       zorder=3, label=f"N={n}")
        f = fits[(fits["load"] == 50) & (fits["y"] == y)].iloc[0]
        xs = np.array([0, d["low_clock_s"].max()])
        ax.plot(xs, f["intercept"] + f["slope"] * xs, color=c.MUTED, linewidth=0.8, linestyle="--", zorder=2)
        ax.set_xlabel("Seconds below 1.5 GHz")
        ax.set_ylabel(label)
        ax.grid(True, axis="both")
    axes[1].legend(loc="upper left", fontsize=7, handletextpad=0.2)
    fig.tight_layout()
    return fig


def main():
    runs = c.load("runs")
    parts_tbl = c.load("parts")
    idle = c.idle_reference(runs)
    r = per_run(runs, idle)

    s = summary(r)
    c.write_csv(s, "rq3_summary")
    c.write_csv(tests(runs), "rq3_tests")
    pt, ps = seq_parts(runs, parts_tbl)
    c.write_csv(pt, "rq3_parts")
    c.write_csv(ps, "rq3_parts_summary")
    _, dm_runs = c.dram_model(runs)
    dm = dm_runs.groupby("case_id")["dram_model_err_pct"].mean().rename("err_pct_mean").reset_index()
    c.write_csv(ram_table(r, parts_tbl, dm), "rq3_ram")

    load_ids = runs.loc[runs["case_id"].isin(SERIES["load50"] + SERIES["load25"]), "run_id"]
    perf = c.load("perf_1s")
    persec = c.perf_seconds(perf[perf["run_id"].isin(load_ids)])
    low, fits = low_clock(runs, persec)
    c.write_csv(low, "rq3_low_clock")
    c.write_csv(fits, "rq3_low_clock_fit")

    # example for the figure: the fastest and the slowest run of load50 N=2
    e = low[low["case_id"] == "rq3_cpu_load50_n2"].sort_values("duration_s")
    c.setup_style()
    c.save_figure(fig_split(s), "rq3_split")
    c.save_figure(fig_partial_load(s), "rq3_partial_load")
    c.save_figure(fig_low_clock(low, fits, persec, e["run_id"].iloc[0], e["run_id"].iloc[-1]), "rq3_low_clock")


if __name__ == "__main__":
    main()
