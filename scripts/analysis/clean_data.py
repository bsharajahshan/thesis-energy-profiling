#!/usr/bin/env python3

"""
clean_data.py - Data Cleaning and Validation

Reads raw CSV results, validates schema, removes outliers, and outputs clean dataset.
"""

import pandas as pd
import sys
from pathlib import Path
from typing import Optional


def load_and_clean(input_csv: str, output_csv: Optional[str] = None) -> pd.DataFrame:
    """
    Load raw results CSV and validate/clean data.

    Expected columns (from CLAUDE.md):
        experiment_id, task_name, mode, subsystem, intensity, task_count,
        duration_s, energy_uj, energy_j, run_id, timestamp

    Steps:
    1. Validate all required columns exist
    2. Convert data types
    3. Remove rows with invalid/missing data
    4. Remove statistical outliers (if CV > 3% in a group, flag for investigation)
    5. Write clean CSV
    """
    try:
        df = pd.read_csv(input_csv)
    except FileNotFoundError:
        print(f"ERROR: File not found: {input_csv}", file=sys.stderr)
        return pd.DataFrame()
    except pd.errors.ParserError as e:
        print(f"ERROR: Failed to parse CSV: {e}", file=sys.stderr)
        return pd.DataFrame()

    # Define required columns
    required_cols = [
        "experiment_id",
        "task_name",
        "mode",
        "subsystem",
        "intensity",
        "task_count",
        "duration_s",
        "energy_uj",
        "energy_j",
        "run_id",
        "timestamp",
    ]

    # Check for missing columns
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        print(f"WARNING: Missing columns: {missing}", file=sys.stderr)

    # Convert data types
    df["duration_s"] = pd.to_numeric(df["duration_s"], errors="coerce")
    df["energy_uj"] = pd.to_numeric(df["energy_uj"], errors="coerce")
    df["energy_j"] = pd.to_numeric(df["energy_j"], errors="coerce")
    df["intensity"] = pd.to_numeric(df["intensity"], errors="coerce")
    df["task_count"] = pd.to_numeric(df["task_count"], errors="coerce")

    # Remove rows with NaN in key columns
    df = df.dropna(subset=["duration_s", "energy_j"])

    # Remove negative or zero values (invalid measurements)
    df = df[(df["duration_s"] > 0) & (df["energy_j"] > 0)]

    # Calculate coefficient of variation per group
    grouped = df.groupby(["task_name", "intensity", "task_count"]).agg({
        "energy_j": ["mean", "std", "count"]
    })
    grouped.columns = ["mean", "std", "count"]
    grouped["cv"] = grouped["std"] / grouped["mean"]

    # Flag high-CV groups
    high_cv = grouped[grouped["cv"] > 0.03]
    if not high_cv.empty:
        print("WARNING: High CV (>3%) in groups:", file=sys.stderr)
        print(high_cv, file=sys.stderr)

    # Write cleaned data
    if output_csv:
        df.to_csv(output_csv, index=False)
        print(f"Cleaned data written to: {output_csv}")

    return df


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: clean_data.py <input.csv> [output.csv]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    df = load_and_clean(input_file, output_file)
    print(f"Cleaned {len(df)} rows")
