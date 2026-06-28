#!/usr/bin/env python3

"""
stats.py - Statistical Analysis of Energy Profiling Results

Computes:
- Mean, stddev, CV per configuration
- Performance-per-watt
- Energy-delay product
- Mann-Whitney U / Welch t-test for comparisons
"""

import pandas as pd
import numpy as np
from scipy import stats as sp_stats
import sys
from typing import Tuple


def compute_summary_stats(df: pd.DataFrame, groupby_cols: list) -> pd.DataFrame:
    """
    Compute summary statistics (mean, stddev, CV) per configuration.

    Args:
        df: DataFrame with energy_j, duration_s columns
        groupby_cols: Columns to group by (e.g., ['task_name', 'intensity'])

    Returns:
        DataFrame with mean, std, count, cv per group
    """
    grouped = df.groupby(groupby_cols).agg({
        "energy_j": ["mean", "std", "count"],
        "duration_s": "mean",
    })
    grouped.columns = ["energy_mean_j", "energy_std_j", "count", "duration_mean_s"]
    grouped["energy_cv"] = grouped["energy_std_j"] / grouped["energy_mean_j"]
    grouped["power_w"] = grouped["energy_mean_j"] / grouped["duration_mean_s"]

    return grouped


def mann_whitney_u_test(group1: np.ndarray, group2: np.ndarray, alpha: float = 0.05) -> dict:
    """
    Perform Mann-Whitney U test (non-parametric).

    Args:
        group1, group2: Arrays of measurements
        alpha: Significance level

    Returns:
        Dict with statistic, p_value, significant (bool)
    """
    stat, p_value = sp_stats.mannwhitneyu(group1, group2, alternative="two-sided")
    return {
        "statistic": stat,
        "p_value": p_value,
        "significant": p_value < alpha,
        "test": "Mann-Whitney U",
    }


def welch_ttest(group1: np.ndarray, group2: np.ndarray, alpha: float = 0.05) -> dict:
    """
    Perform Welch's t-test (parametric, unequal variances).

    Args:
        group1, group2: Arrays of measurements
        alpha: Significance level

    Returns:
        Dict with statistic, p_value, significant (bool)
    """
    stat, p_value = sp_stats.ttest_ind(group1, group2, equal_var=False)
    return {
        "statistic": stat,
        "p_value": p_value,
        "significant": p_value < alpha,
        "test": "Welch t-test",
    }


def energy_delay_product(energy_j: float, duration_s: float) -> float:
    """Compute Energy-Delay Product (EDP = E × T)."""
    return energy_j * duration_s


def performance_per_watt(work_unit: float, energy_j: float) -> float:
    """Compute performance-per-watt (work_unit / energy_j)."""
    return work_unit / energy_j if energy_j > 0 else 0


def compare_configurations(df: pd.DataFrame, config1_name: str, config2_name: str) -> dict:
    """
    Compare two configurations using Mann-Whitney U test.

    Args:
        df: DataFrame with experiment results
        config1_name, config2_name: task_name values to compare

    Returns:
        Dict with test results and effect size
    """
    group1 = df[df["task_name"] == config1_name]["energy_j"].values
    group2 = df[df["task_name"] == config2_name]["energy_j"].values

    if len(group1) == 0 or len(group2) == 0:
        return {"error": f"No data for comparison {config1_name} vs {config2_name}"}

    test_result = mann_whitney_u_test(group1, group2)
    test_result["group1_mean"] = np.mean(group1)
    test_result["group2_mean"] = np.mean(group2)
    test_result["group1_std"] = np.std(group1)
    test_result["group2_std"] = np.std(group2)
    test_result["effect_size_cohen_d"] = (np.mean(group1) - np.mean(group2)) / np.sqrt(
        (np.var(group1) + np.var(group2)) / 2
    )

    return test_result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: stats.py <cleaned.csv>")
        sys.exit(1)

    df = pd.read_csv(sys.argv[1])

    # Summary stats
    print("\n=== Summary Statistics ===")
    summary = compute_summary_stats(df, ["task_name", "intensity"])
    print(summary)

    # Example comparison (if multiple task types exist)
    task_names = df["task_name"].unique()
    if len(task_names) >= 2:
        print(f"\n=== Comparison: {task_names[0]} vs {task_names[1]} ===")
        cmp = compare_configurations(df, task_names[0], task_names[1])
        print(cmp)
