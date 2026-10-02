#!/usr/bin/env python3

"""
load_data.py - Load and unify raw RQ1/RQ2/RQ3 result CSVs

The raw CSV files under results/raw/ have no header row (measure_rapl.sh's
callers just echo comma-separated values), and RQ1/RQ3 share one 11-column
schema while RQ2 has a different 13-column schema (combination instead of
subsystem, plus cpu_util_pct/mem_util_pct). This module reads all of them
into one unified DataFrame, with columns that don't apply to a given RQ left
as NaN, plus an `rq` column derived from experiment_id's prefix.

Column meaning notes:
- `intensity` is intentionally left as a string/object column, not coerced
  to numeric here: it's numeric text for CPU/memory ("1".."8") but
  categorical for I/O ("sequential"/"random") and fixed at "max" for RQ2/RQ3.
  Forcing it to numeric would turn the I/O rows into NaN and lose the
  sequential-vs-random distinction. Callers that need it as a number (e.g.
  plotting CPU/memory load curves) should convert it themselves, scoped to
  just those subsystem's rows.
"""

import re
import sys
from pathlib import Path

import pandas as pd

RQ1_RQ3_COLUMNS = [
    "experiment_id", "task_name", "mode", "subsystem", "intensity",
    "task_count", "duration_s", "energy_uj", "energy_j", "run_id", "timestamp",
]

RQ2_COLUMNS = [
    "experiment_id", "task_name", "mode", "combination", "intensity",
    "task_count", "duration_s", "energy_uj", "energy_j",
    "cpu_util_pct", "mem_util_pct", "run_id", "timestamp",
]


def _read_dir(dir_path: Path, columns: list) -> pd.DataFrame:
    """Read every *.csv under dir_path (recursively, no header) into one frame."""
    files = sorted(dir_path.rglob("*.csv")) if dir_path.exists() else []
    if not files:
        return pd.DataFrame(columns=columns)

    frames = []
    for f in files:
        try:
            frames.append(pd.read_csv(f, header=None, names=columns))
        except pd.errors.ParserError as e:
            print(f"WARNING: skipping unparsable file {f}: {e}", file=sys.stderr)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def load_all_results(results_dir: str = "results/raw") -> pd.DataFrame:
    """
    Load every RQ1/RQ2/RQ3 result CSV under results_dir into one unified
    DataFrame. Missing directories (an RQ that hasn't been run yet) just
    contribute zero rows, not an error.
    """
    base = Path(results_dir)

    rq1 = _read_dir(base / "rq1_baseline", RQ1_RQ3_COLUMNS)
    rq3 = _read_dir(base / "rq3_granularity", RQ1_RQ3_COLUMNS)
    rq2 = _read_dir(base / "rq2_mixed", RQ2_COLUMNS)

    # Unify to one superset schema: subsystem (RQ1/RQ3) and combination (RQ2)
    # are kept as separate columns, NaN where not applicable, rather than
    # merged into one ambiguous column - they mean different things (single
    # subsystem under test vs. which subsystems were combined).
    for df in (rq1, rq3):
        df["combination"] = pd.NA
        df["cpu_util_pct"] = pd.NA
        df["mem_util_pct"] = pd.NA
    rq2["subsystem"] = pd.NA

    combined = pd.concat([rq1, rq2, rq3], ignore_index=True)
    if combined.empty:
        print(f"WARNING: no result CSVs found under {results_dir}", file=sys.stderr)
        return combined

    combined["rq"] = combined["experiment_id"].str.extract(r"^(rq\d)_", expand=False)

    return combined


if __name__ == "__main__":
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results/raw"
    df = load_all_results(results_dir)
    print(f"Loaded {len(df)} rows from {results_dir}")
    if not df.empty:
        print(df["rq"].value_counts())
