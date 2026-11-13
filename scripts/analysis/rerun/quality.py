#!/usr/bin/env python3

"""
quality.py - data quality and spread of the re-run (analysis piece 1)

Reads results/rerun_2026-10-31/tables/ and writes to results/rerun_2026-10-31/analysis/:
  quality_session.csv   facts about the session for chapter 4 (runs, attempts, gaps
                        between windows, conditions, power limits, perf vs RAPL file)
  quality_cv.csv        per case: n, mean, SD and CV of time, package and DRAM energy,
                        package power, clock, cycles and instructions; flags CV > 3 %
  quality_work.csv      per case: work done / target, DRAM data moved, disk bytes / fio bytes
  quality_throttle.csv  per case: runs with throttling, throttle time and its share of
                        the window, peak temperature, highest 1 s package power
  quality_drift.csv     idle at the start against the end; every run relative to its
                        case mean, per round, and the slope over the 5 rounds
  quality_spread.csv    every run of the fixed-work cases with a time CV above 3 %:
                        deviation from the case mean of time, energy, clock, cycles,
                        and throttle time, start temperature, round
  quality_spread_cases.csv  the same cases, one row each: CVs and correlations of time
                        with clock, throttle time, start temperature and round (n = 5,
                        descriptive only)
  quality_clock_fit.csv linear fit of time and of package energy against clock, all
                        relative to the case mean (fixed-work runs with a CPU job)
Figure: thesis/figures/quality_clock_time_energy.pdf (the same runs: time and package
energy against clock)

Nothing here is a test of a research question; the CVs and checks go into chapter 4.

Usage: py scripts/analysis/rerun/quality.py
"""

import os
import sys

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

CV_LIMIT = 3.0       # % (CLAUDE.md: investigate a CV above 3 %)
LOGICAL_CPUS = 8     # thr_core_* sums the 8 logical CPUs, each core counted twice


def fact(rows, key, value, unit="", note=""):
    rows.append({"key": key, "value": value, "unit": unit, "note": note})


def session_facts(runs, attempts):
    rows = []
    fact(rows, "valid_runs", len(runs))
    fact(rows, "cases", runs["case_id"].nunique(), note="52 cases + idle")
    fact(rows, "attempts", len(attempts))
    retried = sorted(attempts.loc[attempts["verdict"] != "VALID", "run_id"].unique())
    fact(rows, "retried_runs", len(retried), note=" ".join(retried))
    fact(rows, "retry_reasons", "; ".join(sorted(attempts.loc[attempts["verdict"] != "VALID", "hard_reasons"]
                                                 .astype(str).unique())))

    win = attempts[attempts["has_window"] == 1].sort_values("t_start_us")
    gaps = (win["t_start_us"].to_numpy()[1:] - win["t_end_us"].to_numpy()[:-1]) / 1e6
    fact(rows, "windows_incl_failed", len(win))
    fact(rows, "smallest_gap_between_windows", round(gaps.min(), 2), "s", "all attempts with a window")
    t0, t1 = win["t_start_us"].min(), win["t_end_us"].max()
    fact(rows, "session_first_to_last_window", round((t1 - t0) / 3.6e9, 2), "h", "all attempts with a window")
    fact(rows, "sum_of_windows", round(runs["duration_s"].sum() / 3600, 2), "h")

    fact(rows, "governor_start", str(runs["governor_start"].value_counts().to_dict()))
    fact(rows, "epp_start", str(runs["epp_start"].value_counts().to_dict()))
    # governor `performance` runs plan no EPP: the kernel sets EPP to `performance` itself
    epp_ok = runs["epp_planned"].isna() | (runs["epp_start"] == runs["epp_planned"])
    same = ((runs["governor_start"] == runs["governor_end"]) & (runs["epp_start"] == runs["epp_end"])
            & (runs["governor_start"] == runs["governor_planned"]) & epp_ok)
    fact(rows, "governor_epp_as_planned_start_and_end", f"{int(same.sum())} of {len(runs)}",
         note="EPP not planned for the governor control runs")
    fact(rows, "power_cable_in_start_and_end", f"{int(((runs['ac_start'] == 1) & (runs['ac_end'] == 1)).sum())} of {len(runs)}")
    for col, label in (("pl1_w_start", "msr_pl1"), ("pl2_w_start", "msr_pl2"),
                       ("mmio_pl1_w_start", "mmio_pl1"), ("mmio_pl2_w_start", "mmio_pl2")):
        vals = sorted(set(runs[col].dropna()) | set(runs[col.replace("_start", "_end")].dropna()))
        fact(rows, label, " ".join(f"{v:g}" for v in vals), "W", "start and end of every run")
    fact(rows, "highest_1s_package_power", round(runs["pkg_power_max_1s_w"].max(), 2), "W")
    fact(rows, "highest_package_temperature", runs["temp_max_c"].max(), "C")

    fact(rows, "temp_start_min_median_max", "{:g} / {:g} / {:g}".format(
        runs["temp_start_c"].min(), runs["temp_start_c"].median(), runs["temp_start_c"].max()), "C")
    fact(rows, "settle_wait_min_median_max", "{:g} / {:g} / {:g}".format(
        runs["settle_s"].min(), runs["settle_s"].median(), runs["settle_s"].max()), "s")
    fact(rows, "settle_timeouts", int((runs["settle_timeout"] == 1).sum()))
    fact(rows, "other_programs_cpu_median_max", "{:.3f} / {:.3f}".format(
        runs["other_cpu_per_s"].median(), runs["other_cpu_per_s"].max()), "CPUs")
    fact(rows, "mem_available_at_start_min", round(runs["mem_avail_start_gib"].min(), 2), "GiB")

    logged = runs[runs["perf_pkg_energy_j"].notna()]
    d_pkg = 100 * (logged["perf_pkg_energy_j"] / logged["pkg_energy_j"] - 1)
    d_dram = 100 * (logged["perf_dram_energy_j"] / logged["dram_energy_j"] - 1)
    fact(rows, "logged_runs", len(logged), note="perf and 1 s sampler on")
    fact(rows, "perf_vs_file_pkg_max_abs", round(d_pkg.abs().max(), 3), "%")
    fact(rows, "perf_vs_file_dram_max_abs", round(d_dram.abs().max(), 3), "%")
    fact(rows, "soft_flags", int(runs["soft_flags"].notna().sum()), note="valid runs with a soft flag")

    thr = (runs["thr_pkg_count"] > 0) | (runs["thr_core_count"] > 0)
    fact(rows, "runs_with_throttling", int(thr.sum()))
    fact(rows, "runs_at_99C_or_more", int((runs["temp_max_c"] >= 99).sum()))
    return pd.DataFrame(rows)


def cv_table(runs):
    cols = ["duration_s", "pkg_energy_j", "dram_energy_j", "pkg_power_w", "dram_power_w",
            "busy_mhz", "busy_cpus", "cycles", "instructions"]
    s = c.summary(runs, cols)
    info = runs.groupby("case_id")[["block", "size", "logging"]].first().reset_index()
    s = info.merge(s, on="case_id")
    for col in ("duration_s", "pkg_energy_j", "dram_energy_j"):
        s[f"{col}_cv_over_3"] = s[f"{col}_cv_pct"] > CV_LIMIT
    return s


def work_table(runs):
    r = runs.copy()
    r["cpu_done_ratio"] = r["cpu_ops_done"] / r["cpu_ops_target"]
    r["vm_done_ratio"] = r["vm_ops_done"] / r["vm_ops_target"]
    r["io_done_ratio"] = r["io_bytes_done"] / r["io_bytes_target"]
    r["dram_gib"] = r["dram_read_gib"] + r["dram_write_gib"]
    r["disk_per_fio_bytes"] = r["disk_written_bytes"] / r["io_bytes_done"]
    g = r.groupby("case_id")
    out = pd.DataFrame({"n": g.size()})
    for col in ("cpu_done_ratio", "vm_done_ratio", "io_done_ratio", "disk_per_fio_bytes"):
        out[f"{col}_min"] = g[col].min()
        out[f"{col}_max"] = g[col].max()
    for col in ("dram_gib", "instructions"):
        out[f"{col}_mean"] = g[col].mean()
        out[f"{col}_cv_pct"] = 100 * g[col].std(ddof=1) / g[col].mean()
    return out.reset_index()


def throttle_table(runs):
    r = runs.copy()
    r["throttled"] = (r["thr_pkg_count"] > 0) | (r["thr_core_count"] > 0)
    r["thr_pkg_share_pct"] = 100 * r["thr_pkg_ms"] / 1000 / r["duration_s"]
    r["thr_core_ms_per_core"] = r["thr_core_ms"] / LOGICAL_CPUS
    r["at_99c"] = r["temp_max_c"] >= 99
    g = r.groupby("case_id")
    out = pd.DataFrame({
        "n": g.size(),
        "runs_throttled": g["throttled"].sum(),
        "thr_pkg_ms_mean": g["thr_pkg_ms"].mean(),
        "thr_pkg_ms_max": g["thr_pkg_ms"].max(),
        "thr_pkg_share_pct_mean": g["thr_pkg_share_pct"].mean(),
        "thr_core_ms_per_core_mean": g["thr_core_ms_per_core"].mean(),
        "temp_max_c_mean": g["temp_max_c"].mean(),
        "temp_max_c_max": g["temp_max_c"].max(),
        "runs_at_99c": g["at_99c"].sum(),
        "pkg_power_max_1s_w_max": g["pkg_power_max_1s_w"].max(),
        "pkg_power_w_mean": g["pkg_power_w"].mean(),
        "busy_mhz_mean": g["busy_mhz"].mean(),
    })
    return out.reset_index().sort_values("thr_pkg_ms_mean", ascending=False)


def relative(runs, cols):
    """Every run's value as % deviation from the mean of its case."""
    r = runs.copy()
    for col in cols:
        r[f"{col}_rel_pct"] = 100 * (r[col] / r.groupby("case_id")[col].transform("mean") - 1)
    return r


def drift_table(runs):
    rows = []
    idle = runs[runs["case_id"] == "idle"]
    start = idle[idle["run_id"].str.startswith("idle_start")]
    end = idle[idle["run_id"].str.startswith("idle_end")]
    for col in ("pkg_power_w", "dram_power_w"):
        w = c.welch(start[col], end[col])
        rows.append({"what": f"idle end vs start: {col}", "value": w["diff_pct"], "lo": w["diff_pct_lo"],
                     "hi": w["diff_pct_hi"], "p": w["p_welch"], "unit": "% of start",
                     "note": f"start {w['mean_ref']:.3f}, end {w['mean_other']:.3f}"})

    rounds = runs[(runs["case_id"] != "idle") & runs["round"].notna()]
    rel = relative(rounds, ["pkg_energy_j", "dram_energy_j", "duration_s"])
    for col, sel in (("pkg_energy_j", rel), ("dram_energy_j", rel),
                     ("duration_s", rel[rel["size"] == "fixed work"])):
        y = sel[f"{col}_rel_pct"]
        for rnd, v in y.groupby(sel["round"]):
            rows.append({"what": f"round {int(rnd)}: {col}", "value": v.mean(), "lo": np.nan, "hi": np.nan,
                         "p": np.nan, "unit": "% of case mean", "note": f"SD {v.std(ddof=1):.2f}, {len(v)} runs"})
        lr = sp_stats.linregress(sel["round"], y)
        half = sp_stats.t.ppf(0.975, len(y) - 2) * lr.stderr
        rows.append({"what": f"slope over rounds: {col}", "value": lr.slope, "lo": lr.slope - half,
                     "hi": lr.slope + half, "p": lr.pvalue, "unit": "% per round",
                     "note": f"{len(y)} runs, linear fit of the deviation from the case mean"})
    return pd.DataFrame(rows)


def spread_tables(runs, cv):
    cases = cv.loc[(cv["size"] == "fixed work") & (cv["duration_s_cv_pct"] > CV_LIMIT), "case_id"]
    cols = ["duration_s", "pkg_energy_j", "dram_energy_j", "busy_mhz", "cycles", "instructions"]
    rel = relative(runs[runs["case_id"].isin(cases)], cols)
    keep = (["case_id", "run_id", "rep", "round", "position", "duration_s", "pkg_energy_j", "busy_mhz",
             "busy_cpus", "thr_pkg_ms", "temp_start_c", "temp_max_c", "settle_s", "other_cpu_per_s",
             "cpu_migrations"] + [f"{col}_rel_pct" for col in cols])
    runs_out = rel[keep].sort_values(["case_id", "rep"])

    rows = []
    for case, g in rel.groupby("case_id"):
        row = {"case_id": case, "n": len(g)}
        for col in cols:
            row[f"{col}_cv_pct"] = c.cv_pct(g[col]) if g[col].notna().all() else np.nan
        for col in ("busy_mhz", "thr_pkg_ms", "temp_start_c", "round", "pkg_energy_j"):
            ok = g[col].notna().all() and g[col].nunique() > 1
            row[f"r_time_{col}"] = np.corrcoef(g["duration_s"], g[col])[0, 1] if ok else np.nan
        rows.append(row)
    cases_out = pd.DataFrame(rows).sort_values("duration_s_cv_pct", ascending=False)
    return runs_out, cases_out


def clock_runs(runs):
    """Logged fixed-work runs with a CPU job (the clock comes from perf; for the
    other jobs the clock does not set the time), relative to their case mean."""
    has_cpu = runs["parts"].str.split("+").apply(lambda p: "cpu" in p)
    sel = runs[(runs["size"] == "fixed work") & runs["busy_mhz"].notna() & has_cpu]
    return relative(sel, ["duration_s", "pkg_energy_j", "busy_mhz"])


def clock_fit(rel):
    rows = []
    for col in ("duration_s", "pkg_energy_j"):
        lr = sp_stats.linregress(rel["busy_mhz_rel_pct"], rel[f"{col}_rel_pct"])
        half = sp_stats.t.ppf(0.975, len(rel) - 2) * lr.stderr
        rows.append({"y": f"{col}_rel_pct", "x": "busy_mhz_rel_pct", "slope": lr.slope,
                     "slope_lo": lr.slope - half, "slope_hi": lr.slope + half, "r": lr.rvalue,
                     "n_runs": len(rel), "n_cases": rel["case_id"].nunique(),
                     "clock_rel_min": rel["busy_mhz_rel_pct"].min(), "clock_rel_max": rel["busy_mhz_rel_pct"].max()})
    return pd.DataFrame(rows)


def figure(rel):
    """Time and package energy against clock, each relative to the case mean."""
    c.setup_style()
    fig, axes = c.plt.subplots(1, 2, figsize=(c.FULL_W, 2.6), sharex=True, sharey=True)
    for ax, col, label in ((axes[0], "duration_s", "Time (% from case mean)"),
                           (axes[1], "pkg_energy_j", "Package energy (% from case mean)")):
        ax.axhline(0, color=c.AXIS, linewidth=0.8, zorder=1)
        ax.axvline(0, color=c.AXIS, linewidth=0.8, zorder=1)
        ax.scatter(rel["busy_mhz_rel_pct"], rel[f"{col}_rel_pct"], s=14, color=c.JOB["cpu"],
                   edgecolor=c.SURFACE, linewidth=0.5, zorder=3)
        ax.set_xlabel("Clock while busy (% from case mean)")
        ax.set_ylabel(label)
        ax.grid(True, axis="both")
    return fig


def main():
    runs = c.load("runs")
    attempts = c.load("attempts")

    c.write_csv(session_facts(runs, attempts), "quality_session")
    cv = cv_table(runs)
    c.write_csv(cv, "quality_cv")
    c.write_csv(work_table(runs), "quality_work")
    c.write_csv(throttle_table(runs), "quality_throttle")
    c.write_csv(drift_table(runs), "quality_drift")
    spread_runs, spread_cases = spread_tables(runs, cv)
    c.write_csv(spread_runs, "quality_spread")
    c.write_csv(spread_cases, "quality_spread_cases")
    rel = clock_runs(runs)
    c.write_csv(clock_fit(rel), "quality_clock_fit")
    c.save_figure(figure(rel), "quality_clock_time_energy")


if __name__ == "__main__":
    main()
