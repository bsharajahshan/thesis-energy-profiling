#!/usr/bin/env python3

"""
stats.py - Statistical Analysis of Energy Profiling Results

Computes:
- Mean, stddev, CV per configuration
- Energy-delay product
- Mann-Whitney U / Welch t-test for comparisons
- RQ2's additivity check (combined vs. sum of isolated RQ1 baselines)
- RQ3's task-splitting overhead (E_parallel - E_sequential per subsystem x N)
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, str(Path(__file__).parent))
from clean_data import load_and_clean  # noqa: E402


def compute_summary_stats(df: pd.DataFrame, groupby_cols: list) -> pd.DataFrame:
    """Compute mean/stddev/CV/power per group, grouped by the given columns."""
    grouped = df.groupby(groupby_cols, dropna=False).agg({
        "energy_j": ["mean", "std", "count"],
        "duration_s": "mean",
        "dram_energy_j": ["mean", "std"],
    })
    grouped.columns = ["energy_mean_j", "energy_std_j", "count", "duration_mean_s",
                       "dram_mean_j", "dram_std_j"]
    grouped["energy_cv"] = grouped["energy_std_j"] / grouped["energy_mean_j"]
    grouped["power_w"] = grouped["energy_mean_j"] / grouped["duration_mean_s"]
    # RAPL's package domain does not include the DRAM domain, so the two add up.
    grouped["dram_power_w"] = grouped["dram_mean_j"] / grouped["duration_mean_s"]
    grouped["total_mean_j"] = grouped["energy_mean_j"] + grouped["dram_mean_j"]

    return grouped


def mann_whitney_u_test(group1: np.ndarray, group2: np.ndarray, alpha: float = 0.05) -> dict:
    """Perform Mann-Whitney U test (non-parametric)."""
    stat, p_value = sp_stats.mannwhitneyu(group1, group2, alternative="two-sided")
    return {
        "statistic": stat,
        "p_value": p_value,
        "significant": p_value < alpha,
        "test": "Mann-Whitney U",
    }


def welch_ttest(group1: np.ndarray, group2: np.ndarray, alpha: float = 0.05) -> dict:
    """Perform Welch's t-test (parametric, unequal variances)."""
    stat, p_value = sp_stats.ttest_ind(group1, group2, equal_var=False)
    return {
        "statistic": stat,
        "p_value": p_value,
        "significant": p_value < alpha,
        "test": "Welch t-test",
    }


def energy_delay_product(energy_j: float, duration_s: float) -> float:
    """Compute Energy-Delay Product (EDP = E x T)."""
    return energy_j * duration_s


def performance_per_watt(work_unit: float, energy_j: float) -> float:
    """
    Compute performance-per-watt (work_unit / energy_j).

    work_unit is deliberately not defined here - what counts as "work" is a
    thesis-writing decision specific to what's being compared (e.g. thread-
    seconds for CPU, GB touched for RAM per the RQ3 work-equivalence fix),
    not something to hardcode into a generic stats helper.
    """
    return work_unit / energy_j if energy_j > 0 else 0


def compare_groups(df: pd.DataFrame, filter1: dict, filter2: dict) -> dict:
    """
    Compare two groups of runs, each selected by an exact-match column
    filter (e.g. {"rq": "rq3", "subsystem": "cpu", "mode": "parallel",
    "task_count": 1} vs the same with "task_count": 8), using Mann-Whitney U
    on their energy_j distributions.
    """
    def apply_filter(frame: pd.DataFrame, f: dict) -> pd.DataFrame:
        mask = pd.Series(True, index=frame.index)
        for key, value in f.items():
            mask &= frame[key] == value
        return frame[mask]

    group1 = apply_filter(df, filter1)["energy_j"].values
    group2 = apply_filter(df, filter2)["energy_j"].values

    if len(group1) == 0 or len(group2) == 0:
        return {"error": f"No data for comparison {filter1} vs {filter2}"}

    result = mann_whitney_u_test(group1, group2)
    result["group1_mean"] = float(np.mean(group1))
    result["group2_mean"] = float(np.mean(group2))
    result["group1_std"] = float(np.std(group1))
    result["group2_std"] = float(np.std(group2))
    if len(group1) > 1 and len(group2) > 1:
        result["effect_size_cohen_d"] = float(
            (np.mean(group1) - np.mean(group2)) / np.sqrt((np.var(group1) + np.var(group2)) / 2)
        )
    return result


def rq2_additivity(df: pd.DataFrame) -> pd.DataFrame:
    """
    RQ2's core question: does combined (parallel) energy differ from the sum
    of the corresponding isolated RQ1 MAX-intensity baselines? Matches each
    RQ2 combination to its constituent subsystems' RQ1 baselines at MAX
    intensity (cpu="8", memory="8", io="sequential" - kept as strings since
    intensity is not numerically coerced, see load_data.py).
    """
    rq1 = df[df["rq"] == "rq1"]
    rq2_parallel = df[(df["rq"] == "rq2") & (df["mode"] == "parallel")]

    max_intensity = {"cpu": "8", "memory": "8", "io": "sequential"}
    combo_subsystems = {
        "cpu_ram": ["cpu", "memory"],
        "cpu_io": ["cpu", "io"],
        "ram_io": ["memory", "io"],
        "cpu_ram_io": ["cpu", "memory", "io"],
    }

    rows = []
    for combo, subsystems in combo_subsystems.items():
        isolated_sum = 0.0
        isolated_dram_sum = 0.0
        missing = []
        for s in subsystems:
            baseline = rq1[(rq1["subsystem"] == s) & (rq1["intensity"].astype(str) == max_intensity[s])]
            if baseline.empty:
                missing.append(s)
                continue
            isolated_sum += baseline["energy_j"].mean()
            isolated_dram_sum += baseline["dram_energy_j"].mean()

        combined = rq2_parallel[rq2_parallel["combination"] == combo]

        row = {"combination": combo, "isolated_sum_j": None, "combined_mean_j": None,
               "delta_pct": None, "dram_isolated_sum_j": None, "dram_combined_mean_j": None,
               "dram_delta_pct": None, "note": ""}
        if missing:
            row["note"] = f"missing RQ1 baseline for: {missing}"
        elif combined.empty:
            row["isolated_sum_j"] = isolated_sum
            row["note"] = "missing RQ2 combined data"
        else:
            combined_mean = combined["energy_j"].mean()
            row["isolated_sum_j"] = isolated_sum
            row["combined_mean_j"] = combined_mean
            row["delta_pct"] = (combined_mean - isolated_sum) / isolated_sum * 100
            dram_combined = combined["dram_energy_j"].mean()
            if pd.notna(dram_combined) and isolated_dram_sum > 0:
                row["dram_isolated_sum_j"] = isolated_dram_sum
                row["dram_combined_mean_j"] = dram_combined
                row["dram_delta_pct"] = (dram_combined - isolated_dram_sum) / isolated_dram_sum * 100
        rows.append(row)

    return pd.DataFrame(rows)


def rq3_overhead(df: pd.DataFrame) -> pd.DataFrame:
    """
    RQ3: parallel vs. sequential energy per (subsystem, N).

    IMPORTANT interpretation note: E_parallel - E_sequential only isolates
    "pure per-task launch overhead" (the original DEVLOG design intent) at
    N=1, where both modes run one task for the same duration and the
    difference is purely the subshell/backgrounding code path. For N>1 it
    does NOT cleanly isolate overhead - sequential's total is dominated by
    running N sub-tasks back-to-back (N x the wall-clock, N x the total
    task-time), so the gap mostly reflects "doing N times more total work
    over N times as long," which is really restating RQ3's core finding
    (concurrent execution is more efficient), not a separate overhead term.
    Reported as raw numbers for both modes rather than a single misleadingly
    -named "overhead" figure - don't over-interpret the N>1 gap as launch
    overhead without accounting for this.
    """
    rq3 = df[df["rq"] == "rq3"]
    rows = []
    # intensity separates the main 100% runs ("max") from partial-load CPU
    # variants ("load50", ...) - they must never be pooled.
    for (subsystem, intensity), sub in rq3.groupby(["subsystem", "intensity"]):
        for n in sorted(sub["task_count"].dropna().unique()):
            par = sub[(sub["task_count"] == n) & (sub["mode"] == "parallel")]
            seq = sub[(sub["task_count"] == n) & (sub["mode"] == "sequential")]
            if par.empty or seq.empty:
                continue
            par_mean = par["energy_j"].mean()
            seq_mean = seq["energy_j"].mean()
            rows.append({
                "subsystem": subsystem,
                "intensity": intensity,
                "task_count": int(n),
                "parallel_mean_j": par_mean,
                "sequential_mean_j": seq_mean,
                "gap_j": par_mean - seq_mean,
                "parallel_dram_j": par["dram_energy_j"].mean(),
                "sequential_dram_j": seq["dram_energy_j"].mean(),
                "gap_is_pure_launch_overhead": bool(n == 1),
                "parallel_duration_s": par["duration_s"].mean(),
                "sequential_duration_s": seq["duration_s"].mean(),
            })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results/raw"
    df = load_and_clean(results_dir)

    if df.empty:
        print(f"No data found under {results_dir}")
        sys.exit(1)

    print("\n=== RQ1: summary stats per subsystem x intensity ===")
    rq1 = df[df["rq"] == "rq1"]
    if not rq1.empty:
        print(compute_summary_stats(rq1, ["subsystem", "intensity"]))

    print("\n=== RQ2: summary stats per combination x mode ===")
    rq2 = df[df["rq"] == "rq2"]
    if not rq2.empty:
        print(compute_summary_stats(rq2, ["combination", "mode"]))

    print("\n=== RQ2: additivity check (combined vs. sum of isolated RQ1 baselines) ===")
    additivity = rq2_additivity(df)
    print(additivity.to_string(index=False))

    print("\n=== RQ3: summary stats per subsystem x intensity (load level) x mode x task_count ===")
    rq3 = df[df["rq"] == "rq3"]
    if not rq3.empty:
        print(compute_summary_stats(rq3, ["subsystem", "intensity", "mode", "task_count"]))

    print("\n=== RQ3: parallel vs sequential energy per subsystem x N ===")
    print("(gap_j only isolates pure launch overhead where gap_is_pure_launch_overhead is True, i.e. N=1 - see rq3_overhead docstring)")
    overhead = rq3_overhead(df)
    print(overhead.to_string(index=False))
