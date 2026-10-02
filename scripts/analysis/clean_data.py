#!/usr/bin/env python3

"""
clean_data.py - Data Cleaning and Validation

Loads every raw RQ1/RQ2/RQ3 result CSV (via load_data.load_all_results),
validates and coerces types, drops invalid rows, and flags high-variance
configurations. Outputs one clean, unified dataset.
"""

import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from load_data import load_all_results  # noqa: E402


def load_and_clean(results_dir: str = "results/raw", output_csv: Optional[str] = None) -> pd.DataFrame:
    """
    Load all raw results and validate/clean them.

    Steps:
    1. Load and unify RQ1/RQ2/RQ3 CSVs (load_data.load_all_results)
    2. Convert numeric columns (duration_s, energy_uj, energy_j, task_count,
       cpu_util_pct, mem_util_pct) - NOT intensity, which is intentionally
       left as text since it's categorical for I/O and "max" for RQ2/RQ3
    3. Drop rows with invalid/missing duration_s or energy_j
    4. Flag (not silently drop) high-CV configurations for investigation
    5. Optionally write the clean CSV
    """
    df = load_all_results(results_dir)
    if df.empty:
        return df

    numeric_cols = ["duration_s", "energy_uj", "energy_j", "task_count", "cpu_util_pct", "mem_util_pct"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Remove rows with invalid duration/energy (missing, zero, or negative -
    # e.g. the old pre-RAPL-readable zero-energy rows)
    df = df.dropna(subset=["duration_s", "energy_j"])
    df = df[(df["duration_s"] > 0) & (df["energy_j"] > 0)]

    # Flag (don't auto-remove) high-variance configurations. Grouping uses
    # every identifying column together, with dropna=False since subsystem
    # is NaN for RQ2 rows and combination is NaN for RQ1/RQ3 rows - dropping
    # NaN groups (pandas' groupby default) would silently exclude them all.
    # intensity must be in the key: RQ1 writes task_count=1 for every load
    # level, so without it the 1/2/4/8-thread runs (deliberately different
    # loads) pool into one group and produce a meaningless, inflated CV.
    group_cols = ["rq", "task_name", "mode", "subsystem", "combination", "intensity", "task_count"]
    grouped = df.groupby(group_cols, dropna=False)["energy_j"].agg(["mean", "std", "count"])
    grouped["cv"] = grouped["std"] / grouped["mean"]

    high_cv = grouped[grouped["cv"] > 0.03]
    if not high_cv.empty:
        print("WARNING: High CV (>3%) in groups - investigate before trusting these:", file=sys.stderr)
        print(high_cv, file=sys.stderr)

    if output_csv:
        df.to_csv(output_csv, index=False)
        print(f"Cleaned data written to: {output_csv}")

    return df


if __name__ == "__main__":
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results/raw"
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    df = load_and_clean(results_dir, output_file)
    print(f"Cleaned {len(df)} rows")
    if not df.empty:
        print(df["rq"].value_counts())
