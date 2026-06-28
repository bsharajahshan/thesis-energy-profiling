#!/usr/bin/env python3

"""
plot.py - Data Visualization and Plot Generation

Generates plots for thesis:
- rq1_load_curves.pdf      — energy vs load level per subsystem
- rq2_energy_comparison.pdf — grouped bar: sequential vs parallel configs
- rq2_runtime_comparison.pdf — runtime comparison
- rq3_efficiency_curve.pdf  — efficiency vs task count N
"""

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
from pathlib import Path


def setup_plot_style():
    """Configure matplotlib for thesis publication."""
    plt.style.use("seaborn-v0_8-darkgrid")
    plt.rcParams["figure.figsize"] = (10, 6)
    plt.rcParams["font.size"] = 11
    plt.rcParams["axes.labelsize"] = 12
    plt.rcParams["axes.titlesize"] = 12
    plt.rcParams["xtick.labelsize"] = 10
    plt.rcParams["ytick.labelsize"] = 10
    plt.rcParams["legend.fontsize"] = 10


def plot_rq1_load_curves(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ1: Energy vs load level per subsystem.

    Plots energy (J) on y-axis, load level on x-axis, separate line per subsystem.
    """
    setup_plot_style()
    fig, ax = plt.subplots()

    for subsystem in df["subsystem"].unique():
        subset = df[df["subsystem"] == subsystem].groupby("intensity").agg({
            "energy_j": ["mean", "std"],
            "duration_s": "mean"
        }).reset_index()

        intensity = subset["intensity"].values
        energy_mean = subset[("energy_j", "mean")].values
        energy_std = subset[("energy_j", "std")].values

        ax.errorbar(intensity, energy_mean, yerr=energy_std, marker="o", label=subsystem)

    ax.set_xlabel("Load Level (Intensity)")
    ax.set_ylabel("Energy (J)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    output_path = Path(output_dir) / "rq1_load_curves.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq2_energy_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ2: Energy consumption comparison (sequential vs parallel).

    Grouped bar chart: configuration on x-axis, energy on y-axis.
    """
    setup_plot_style()
    fig, ax = plt.subplots()

    modes = df["mode"].unique()
    configs = df[df["mode"].isin(modes)].groupby("mode").agg({
        "energy_j": ["mean", "std"]
    }).reset_index()

    x = np.arange(len(configs))
    width = 0.35

    energy_mean = configs[("energy_j", "mean")].values
    energy_std = configs[("energy_j", "std")].values

    ax.bar(x, energy_mean, width, yerr=energy_std, capsize=5)
    ax.set_xlabel("Configuration")
    ax.set_ylabel("Energy (J)")
    ax.set_xticks(x)
    ax.set_xticklabels(configs["mode"].values, rotation=45)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    output_path = Path(output_dir) / "rq2_energy_comparison.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq2_runtime_comparison(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ2: Runtime comparison (sequential vs parallel).
    """
    setup_plot_style()
    fig, ax = plt.subplots()

    modes = df["mode"].unique()
    configs = df[df["mode"].isin(modes)].groupby("mode").agg({
        "duration_s": ["mean", "std"]
    }).reset_index()

    x = np.arange(len(configs))
    width = 0.35

    duration_mean = configs[("duration_s", "mean")].values
    duration_std = configs[("duration_s", "std")].values

    ax.bar(x, duration_mean, width, yerr=duration_std, capsize=5, color="orange")
    ax.set_xlabel("Configuration")
    ax.set_ylabel("Runtime (s)")
    ax.set_xticks(x)
    ax.set_xticklabels(configs["mode"].values, rotation=45)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    output_path = Path(output_dir) / "rq2_runtime_comparison.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


def plot_rq3_efficiency_curve(df: pd.DataFrame, output_dir: str = "figures"):
    """
    RQ3: Efficiency vs task count (granularity sweep).

    Plots energy per task (or some work unit) on y-axis, task count on x-axis.
    """
    setup_plot_style()
    fig, ax = plt.subplots()

    # Assuming task_count column exists
    if "task_count" in df.columns:
        subset = df.groupby("task_count").agg({
            "energy_j": ["mean", "std"],
            "duration_s": "mean"
        }).reset_index()

        task_counts = subset["task_count"].values
        energy_mean = subset[("energy_j", "mean")].values
        energy_std = subset[("energy_j", "std")].values

        ax.errorbar(task_counts, energy_mean, yerr=energy_std, marker="s", markersize=8)
        ax.set_xlabel("Task Count (N)")
        ax.set_ylabel("Total Energy (J)")
        ax.set_xscale("log")
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    output_path = Path(output_dir) / "rq3_efficiency_curve.pdf"
    output_path.parent.mkdir(exist_ok=True)
    plt.savefig(output_path, format="pdf")
    print(f"Saved: {output_path}")
    plt.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: plot.py <cleaned.csv> [output_dir]")
        sys.exit(1)

    df = pd.read_csv(sys.argv[1])
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "figures"

    print(f"Generating plots from {sys.argv[1]}...")

    # Generate all plots if data contains relevant columns
    if "subsystem" in df.columns:
        plot_rq1_load_curves(df, output_dir)

    if "mode" in df.columns:
        plot_rq2_energy_comparison(df, output_dir)
        plot_rq2_runtime_comparison(df, output_dir)

    if "task_count" in df.columns:
        plot_rq3_efficiency_curve(df, output_dir)

    print("Done!")
