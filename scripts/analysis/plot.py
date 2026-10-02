#!/usr/bin/env python3

"""
plot.py - Data Visualization and Plot Generation

Generates plots for thesis:
- rq1_load_curves.pdf        — energy vs load level, one panel per subsystem
- rq2_energy_comparison.pdf  — grouped bar: sequential vs parallel, per combination
- rq2_runtime_comparison.pdf — same, but runtime
- rq2_additivity.pdf         — RQ2's actual finding: combined (parallel) energy vs.
                                the sum of the matching isolated RQ1 baselines, per
                                combination. Not in the original CLAUDE.md plot list -
                                added because rq2_energy_comparison.pdf only compares
                                RQ2's own sequential vs parallel (not a fair comparison,
                                since sequential runs 2-3x longer), while the subadditivity
                                finding stats.py's rq2_additivity() computes was never
                                visualized at all.
- rq3_efficiency_curve.pdf   — energy vs task count N (parallel mode), per subsystem

No titles on any plot (captions go in the thesis text, per CLAUDE.md); axis
labels and error bars (+-1 stddev) throughout.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from clean_data import load_and_clean  # noqa: E402
from stats import rq2_additivity  # noqa: E402


def setup_plot_style():
    """Configure matplotlib for thesis publication - clean, no dark theme."""
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "white"
    plt.rcParams["font.size"] = 11
    plt.rcParams["axes.labelsize"] = 12
    plt.rcParams["xtick.labelsize"] = 10
    plt.rcParams["ytick.labelsize"] = 10
    plt.rcParams["legend.fontsize"] = 10
    plt.rcParams["axes.spines.top"] = False
    plt.rcParams["axes.spines.right"] = False


def plot_rq1_load_curves(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ1: energy vs load level, one panel per subsystem - CPU/memory have a
    numeric intensity (thread count / GB) and get a line+errorbar curve; I/O's
    intensity is categorical ("sequential"/"random") and gets a bar chart.
    They can't share one x-axis (different units entirely), so this is three
    small multiples in one figure rather than one shared plot.
    """
    setup_plot_style()
    rq1 = df[df["rq"] == "rq1"]
    if rq1.empty:
        print("No RQ1 data - skipping rq1_load_curves.pdf")
        return

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    numeric_subsystems = [("cpu", "CPU threads", axes[0]), ("memory", "Memory (GB)", axes[1])]
    for subsystem, xlabel, ax in numeric_subsystems:
        subset = rq1[rq1["subsystem"] == subsystem].copy()
        if subset.empty:
            continue
        subset["intensity_num"] = pd.to_numeric(subset["intensity"], errors="coerce")
        grouped = subset.groupby("intensity_num").agg(
            energy_mean=("energy_j", "mean"), energy_std=("energy_j", "std")
        ).reset_index().sort_values("intensity_num")
        ax.errorbar(grouped["intensity_num"], grouped["energy_mean"], yerr=grouped["energy_std"],
                    marker="o", capsize=4)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Energy (J)")
        ax.grid(True, alpha=0.3, axis="y")

    io_ax = axes[2]
    io_subset = rq1[rq1["subsystem"] == "io"]
    if not io_subset.empty:
        grouped = io_subset.groupby("intensity").agg(
            energy_mean=("energy_j", "mean"), energy_std=("energy_j", "std")
        ).reset_index()
        x = np.arange(len(grouped))
        io_ax.bar(x, grouped["energy_mean"], yerr=grouped["energy_std"], capsize=4, width=0.5)
        io_ax.set_xticks(x)
        io_ax.set_xticklabels(grouped["intensity"])
        io_ax.set_xlabel("I/O pattern")
        io_ax.set_ylabel("Energy (J)")
        io_ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    output_path = Path(output_dir) / "rq1_load_curves.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def _rq2_grouped_bar(df: pd.DataFrame, value_col: str, ylabel: str, output_name: str, output_dir: str, color=None):
    """Shared implementation for RQ2's energy/runtime comparison plots."""
    setup_plot_style()
    rq2 = df[df["rq"] == "rq2"]
    if rq2.empty:
        print(f"No RQ2 data - skipping {output_name}")
        return

    combos = ["cpu_ram", "cpu_io", "ram_io", "cpu_ram_io"]  # fixed order, matches CLAUDE.md
    modes = ["sequential", "parallel"]
    width = 0.35
    x = np.arange(len(combos))

    fig, ax = plt.subplots(figsize=(10, 6))
    for i, mode in enumerate(modes):
        means, stds = [], []
        for combo in combos:
            subset = rq2[(rq2["combination"] == combo) & (rq2["mode"] == mode)][value_col]
            means.append(subset.mean() if not subset.empty else 0)
            stds.append(subset.std() if len(subset) > 1 else 0)
        offset = (i - 0.5) * width
        ax.bar(x + offset, means, width, yerr=stds, capsize=4, label=mode,
               color=color[i] if color else None)

    ax.set_xlabel("Combination")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(combos)
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    output_path = Path(output_dir) / output_name
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq2_energy_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """RQ2: energy per combination, sequential vs parallel bars."""
    _rq2_grouped_bar(df, "energy_j", "Energy (J)", "rq2_energy_comparison.pdf", output_dir)


def plot_rq2_runtime_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """RQ2: runtime per combination, sequential vs parallel bars."""
    _rq2_grouped_bar(df, "duration_s", "Runtime (s)", "rq2_runtime_comparison.pdf", output_dir,
                      color=["#eb6834", "#2a78d6"])


def plot_rq2_additivity(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ2's actual finding: combined (parallel) energy vs. the sum of the
    matching isolated RQ1 baselines, per combination. "Isolated sum" is a
    derived value (sum of two/three baseline means), not a raw measured
    distribution, so it gets no error bar; "combined" is a real 5-rep mean
    and keeps its error bar. Delta % is labeled above each pair since that's
    the actual story, not just the two bar heights.
    """
    setup_plot_style()
    additivity = rq2_additivity(df)
    additivity = additivity.dropna(subset=["isolated_sum_j", "combined_mean_j"])
    if additivity.empty:
        print("No RQ2 additivity data - skipping rq2_additivity.pdf")
        return

    rq2 = df[(df["rq"] == "rq2") & (df["mode"] == "parallel")]
    combined_std = {
        combo: rq2[rq2["combination"] == combo]["energy_j"].std()
        for combo in additivity["combination"]
    }

    x = np.arange(len(additivity))
    width = 0.35
    fig, ax = plt.subplots(figsize=(10, 6))

    ax.bar(x - width / 2, additivity["isolated_sum_j"], width, label="Sum of isolated baselines",
           color="#c3c2b7")
    ax.bar(x + width / 2, additivity["combined_mean_j"], width, label="Combined (parallel)",
           yerr=[combined_std[c] for c in additivity["combination"]], capsize=4, color="#2a78d6")

    for i, row in additivity.reset_index(drop=True).iterrows():
        top = max(row["isolated_sum_j"], row["combined_mean_j"])
        ax.text(i, top + top * 0.03, f"{row['delta_pct']:+.0f}%", ha="center", fontsize=10)

    ax.set_xlabel("Combination")
    ax.set_ylabel("Energy (J)")
    ax.set_xticks(x)
    ax.set_xticklabels(additivity["combination"])
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")
    ax.margins(y=0.12)

    plt.tight_layout()
    output_path = Path(output_dir) / "rq2_additivity.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq3_efficiency_curve(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ3: energy vs task count N, parallel mode only (the main experiment),
    one panel per subsystem - CPU and RAM aren't directly comparable (RAM
    is deliberately not bandwidth-capped, see CLAUDE.md), so they get
    separate panels rather than being overlaid on one axis.
    """
    setup_plot_style()
    rq3 = df[(df["rq"] == "rq3") & (df["mode"] == "parallel")]
    if rq3.empty:
        print("No RQ3 parallel data - skipping rq3_efficiency_curve.pdf")
        return

    subsystems = [s for s in ["cpu", "ram"] if s in rq3["subsystem"].unique()]
    fig, axes = plt.subplots(1, len(subsystems), figsize=(6 * len(subsystems), 5), squeeze=False)
    axes = axes[0]

    for ax, subsystem in zip(axes, subsystems):
        subset = rq3[rq3["subsystem"] == subsystem]
        grouped = subset.groupby("task_count").agg(
            energy_mean=("energy_j", "mean"), energy_std=("energy_j", "std")
        ).reset_index().sort_values("task_count")

        ax.errorbar(grouped["task_count"], grouped["energy_mean"], yerr=grouped["energy_std"],
                     marker="s", markersize=8, capsize=4)
        ax.set_xlabel(f"Task count N ({subsystem.upper()})")
        ax.set_ylabel("Energy (J)")
        ax.set_xticks(grouped["task_count"])
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    output_path = Path(output_dir) / "rq3_efficiency_curve.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


if __name__ == "__main__":
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results/raw"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "figures"

    df = load_and_clean(results_dir)
    if df.empty:
        print(f"No data found under {results_dir}")
        sys.exit(1)

    print(f"Generating plots from {results_dir}...")

    plot_rq1_load_curves(df, output_dir)
    plot_rq2_energy_comparison(df, output_dir)
    plot_rq2_runtime_comparison(df, output_dir)
    plot_rq2_additivity(df, output_dir)
    plot_rq3_efficiency_curve(df, output_dir)

    print("Done!")
