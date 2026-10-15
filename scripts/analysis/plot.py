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

DRAM (RAPL intel-rapl:0:2) is a separate domain that the package reading does
not include. Plots therefore show package energy and DRAM energy side by side
(top/bottom rows or stacked bars), never mixed into one number without labeling.

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


def start_y_at_zero(fig):
    """Supervisor requirement: every y-axis starts at 0 (keeps the upper limit matplotlib chose)."""
    for ax in fig.axes:
        ax.set_ylim(bottom=0)


def plot_rq1_load_curves(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ1: energy vs load level, one column per subsystem. Top row: package
    energy. Bottom row: DRAM-only energy (separate RAPL domain). CPU/memory
    have a numeric intensity (thread count / GB) and get a line+errorbar
    curve; I/O's intensity is categorical ("sequential"/"random") and gets a
    bar chart. They can't share one x-axis (different units entirely), so
    this is small multiples rather than one shared plot.
    """
    setup_plot_style()
    rq1 = df[df["rq"] == "rq1"]
    if rq1.empty:
        print("No RQ1 data - skipping rq1_load_curves.pdf")
        return

    fig, axes = plt.subplots(2, 3, figsize=(15, 9))
    rows = [("energy_j", "Package energy (J)"), ("dram_energy_j", "DRAM energy (J)")]

    for r, (col, ylabel) in enumerate(rows):
        for c, (subsystem, xlabel) in enumerate([("cpu", "CPU threads"), ("memory", "Memory (GB)")]):
            ax = axes[r][c]
            subset = rq1[rq1["subsystem"] == subsystem].copy()
            if subset.empty:
                continue
            subset["intensity_num"] = pd.to_numeric(subset["intensity"], errors="coerce")
            grouped = subset.groupby("intensity_num").agg(
                energy_mean=(col, "mean"), energy_std=(col, "std")
            ).reset_index().sort_values("intensity_num")
            ax.errorbar(grouped["intensity_num"], grouped["energy_mean"], yerr=grouped["energy_std"],
                        marker="o", capsize=4)
            ax.set_xticks(grouped["intensity_num"])
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3, axis="y")

        ax = axes[r][2]
        io_subset = rq1[rq1["subsystem"] == "io"]
        if not io_subset.empty:
            grouped = io_subset.groupby("intensity").agg(
                energy_mean=(col, "mean"), energy_std=(col, "std")
            ).reset_index()
            x = np.arange(len(grouped))
            ax.bar(x, grouped["energy_mean"], yerr=grouped["energy_std"], capsize=4, width=0.5)
            ax.set_xticks(x)
            ax.set_xticklabels(grouped["intensity"])
            ax.set_xlabel("I/O pattern")
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3, axis="y")

    start_y_at_zero(fig)
    plt.tight_layout()
    output_path = Path(output_dir) / "rq1_load_curves.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def _rq2_grouped_bar(df: pd.DataFrame, value_col: str, ylabel: str, output_name: str, output_dir: str,
                      color=None, dram_col: str = None):
    """
    Shared implementation for RQ2's energy/runtime comparison plots. With
    dram_col set, each bar is stacked: package energy at the bottom, DRAM
    energy (separate RAPL domain, added on top) above it. The error bar is
    +-1 stddev of the package value only.
    """
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
    base_colors = color or ["#eb6834", "#2a78d6"]
    for i, mode in enumerate(modes):
        means, stds, drams = [], [], []
        for combo in combos:
            sel = rq2[(rq2["combination"] == combo) & (rq2["mode"] == mode)]
            subset = sel[value_col]
            means.append(subset.mean() if not subset.empty else 0)
            stds.append(subset.std() if len(subset) > 1 else 0)
            if dram_col:
                drams.append(sel[dram_col].mean() if not sel.empty else 0)
        offset = (i - 0.5) * width
        label = f"{mode} (package)" if dram_col else mode
        ax.bar(x + offset, means, width, yerr=stds, capsize=4, label=label, color=base_colors[i])
        if dram_col:
            ax.bar(x + offset, drams, width, bottom=means, label=f"{mode} (DRAM)",
                   color=base_colors[i], alpha=0.45, hatch="//", edgecolor="white")

    ax.set_xlabel("Combination")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(combos)
    ax.legend()
    ax.grid(True, alpha=0.3, axis="y")

    start_y_at_zero(fig)
    plt.tight_layout()
    output_path = Path(output_dir) / output_name
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq2_energy_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """RQ2: energy per combination, sequential vs parallel, stacked package + DRAM."""
    _rq2_grouped_bar(df, "energy_j", "Energy (J)", "rq2_energy_comparison.pdf", output_dir,
                      dram_col="dram_energy_j")


def plot_rq2_runtime_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """RQ2: runtime per combination, sequential vs parallel bars."""
    _rq2_grouped_bar(df, "duration_s", "Runtime (s)", "rq2_runtime_comparison.pdf", output_dir,
                      color=["#eb6834", "#2a78d6"])


def plot_rq2_additivity(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ2's actual finding: combined (parallel) energy vs. the sum of the
    matching isolated RQ1 baselines, per combination. Left panel: package
    energy. Right panel: DRAM-only energy. "Isolated sum" is a derived value
    (sum of two/three baseline means), not a raw measured distribution, so it
    gets no error bar; "combined" is a real 5-rep mean and keeps its error
    bar. Delta % is labeled above each pair since that's the actual story,
    not just the two bar heights.

    Caveat for the thesis text: every isolated run contains the machine's
    baseline draw, so the isolated sum counts it 2-3 times and the combined
    run once. No idle measurement exists to subtract it.
    """
    setup_plot_style()
    additivity = rq2_additivity(df)
    additivity = additivity.dropna(subset=["isolated_sum_j", "combined_mean_j"])
    if additivity.empty:
        print("No RQ2 additivity data - skipping rq2_additivity.pdf")
        return

    rq2 = df[(df["rq"] == "rq2") & (df["mode"] == "parallel")]
    panels = [
        ("Package energy (J)", "isolated_sum_j", "combined_mean_j", "delta_pct", "energy_j"),
        ("DRAM energy (J)", "dram_isolated_sum_j", "dram_combined_mean_j", "dram_delta_pct", "dram_energy_j"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))

    width = 0.35
    for ax, (ylabel, iso_col, comb_col, delta_col, raw_col) in zip(axes, panels):
        panel = additivity.dropna(subset=[iso_col, comb_col]).reset_index(drop=True)
        if panel.empty:
            ax.set_visible(False)
            continue
        px = np.arange(len(panel))
        combined_std = [rq2[rq2["combination"] == c][raw_col].std() for c in panel["combination"]]
        ax.bar(px - width / 2, panel[iso_col], width, label="Sum of isolated baselines", color="#c3c2b7")
        ax.bar(px + width / 2, panel[comb_col], width, label="Combined (parallel)",
               yerr=combined_std, capsize=4, color="#2a78d6")
        for i, row in panel.iterrows():
            top = max(row[iso_col], row[comb_col])
            ax.text(i, top + top * 0.03, f"{row[delta_col]:+.0f}%", ha="center", fontsize=10)
        ax.set_xlabel("Combination")
        ax.set_ylabel(ylabel)
        ax.set_xticks(px)
        ax.set_xticklabels(panel["combination"])
        ax.legend()
        ax.grid(True, alpha=0.3, axis="y")
        ax.margins(y=0.12)

    start_y_at_zero(fig)
    plt.tight_layout()
    output_path = Path(output_dir) / "rq2_additivity.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq3_efficiency_curve(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ3: energy vs task count N, parallel mode only (the main experiment),
    one column per subsystem - CPU and RAM aren't directly comparable (RAM
    is deliberately not bandwidth-capped, see CLAUDE.md), so they get
    separate panels rather than being overlaid on one axis. Top row: package
    energy. Bottom row: DRAM-only energy.
    """
    setup_plot_style()
    rq3 = df[(df["rq"] == "rq3") & (df["mode"] == "parallel")]
    if rq3.empty:
        print("No RQ3 parallel data - skipping rq3_efficiency_curve.pdf")
        return

    subsystems = [s for s in ["cpu", "ram"] if s in rq3["subsystem"].unique()]
    fig, axes = plt.subplots(2, len(subsystems), figsize=(6 * len(subsystems), 9), squeeze=False)
    rows = [("energy_j", "Package energy (J)"), ("dram_energy_j", "DRAM energy (J)")]

    for r, (col, ylabel) in enumerate(rows):
        for c, subsystem in enumerate(subsystems):
            ax = axes[r][c]
            subset = rq3[rq3["subsystem"] == subsystem]
            # One line per load level: "max" = the main 100% runs, "loadNN" =
            # partial-load CPU variant (--cpu_load NN). Never pooled.
            intensities = sorted(subset["intensity"].astype(str).unique(), key=lambda i: i != "max")
            for intensity in intensities:
                grouped = subset[subset["intensity"].astype(str) == intensity].groupby("task_count").agg(
                    energy_mean=(col, "mean"), energy_std=(col, "std")
                ).reset_index().sort_values("task_count")
                label = "100% load" if intensity == "max" else f"{intensity.removeprefix('load')}% load"
                ax.errorbar(grouped["task_count"], grouped["energy_mean"], yerr=grouped["energy_std"],
                             marker="s", markersize=8, capsize=4, label=label)
                ax.set_xticks(grouped["task_count"])
            if len(intensities) > 1:
                ax.legend()
            ax.set_xlabel(f"Task count N ({subsystem.upper()})")
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3)

    start_y_at_zero(fig)
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
