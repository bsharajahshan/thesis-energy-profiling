#!/usr/bin/env python3

"""
common.py - shared code for the analysis of the fixed-work re-run

Every analysis script in this folder imports this file. It holds:
  - where the data is and where the results go
  - loading the tables in results/rerun_2026-10-31/tables/ (made by extract_rerun.py)
  - the idle reference and the energy above idle ("active energy")
  - the statistics agreed with the user (2026-10-29, 2026-10-31):
      main test   Welch t-test, two-sided
      correction  Holm, over all tests of one family (one research question,
                  the mechanism block, the controls)
      check       exact Mann-Whitney U, two-sided, not corrected
                  (smallest possible p with 5 vs 5 runs: 2/252 = 0.0079)
      effect size difference of the means, in units and in % of the reference
                  mean, with a 95 % Welch confidence interval
  - the plot style: colours, sizes, saving figures

Standard deviation is always the sample SD (ddof=1). Package and DRAM energy
are separate RAPL domains and are never added here.

Outputs: CSV tables in results/rerun_2026-10-31/analysis/, figures as PDF in
thesis/figures/ (used by the thesis) and as PNG in analysis/preview/ (for
looking at them).

Tests: py scripts/analysis/rerun/test_common.py
"""

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sp_stats  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "results" / "rerun_2026-10-31"
TABLES = DATA / "tables"
OUT = DATA / "analysis"
PREVIEW = OUT / "preview"
FIGURES = REPO / "thesis" / "figures"

ALPHA = 0.05
CONF = 0.95


# ---- data -------------------------------------------------------------------

def load(name, tables=TABLES):
    """One table of tables/ (runs, parts, cases, attempts, samples_1s, perf_1s)."""
    return pd.read_csv(Path(tables) / f"{name}.csv")


def values(runs, case_id, col):
    """The values of one column for one case, in repetition order."""
    sel = runs.loc[runs["case_id"] == case_id].sort_values(["rep", "run_id"])
    if sel.empty:
        raise KeyError(f"no runs for case {case_id!r}")
    return sel[col].to_numpy(dtype=float)


def by_rep(runs, case_id, col):
    """One column of one case as a Series indexed by rep (= round).

    Used to add isolated runs of the same round (RQ2 sum of isolated jobs).
    The idle case has every rep twice (start and end), so it is refused.
    """
    sel = runs.loc[runs["case_id"] == case_id]
    if sel.empty:
        raise KeyError(f"no runs for case {case_id!r}")
    if sel["rep"].duplicated().any():
        raise ValueError(f"case {case_id!r} has a rep more than once")
    return sel.set_index("rep")[col].astype(float).sort_index()


def idle_reference(runs):
    """Idle package and DRAM power: mean of all idle runs, and of the start/end runs."""
    idle = runs.loc[runs["case_id"] == "idle"]
    if idle.empty:
        raise KeyError("no idle runs")
    start = idle[idle["run_id"].str.startswith("idle_start")]
    end = idle[idle["run_id"].str.startswith("idle_end")]
    ref = {"n": len(idle), "n_start": len(start), "n_end": len(end)}
    for dom in ("pkg", "dram"):
        col = f"{dom}_power_w"
        ref[f"{dom}_w"] = idle[col].mean()
        ref[f"{dom}_w_sd"] = idle[col].std(ddof=1)
        ref[f"{dom}_w_start"] = start[col].mean()
        ref[f"{dom}_w_end"] = end[col].mean()
    return ref


def add_active(runs, idle):
    """Add the idle part (idle power x window) and the energy above it, per domain.

    pkg_idle_j + pkg_active_j = pkg_energy_j, the same for DRAM.
    """
    out = runs.copy()
    for dom in ("pkg", "dram"):
        out[f"{dom}_idle_j"] = idle[f"{dom}_w"] * out["duration_s"]
        out[f"{dom}_active_j"] = out[f"{dom}_energy_j"] - out[f"{dom}_idle_j"]
    return out


# ---- statistics -------------------------------------------------------------

def cv_pct(x):
    x = np.asarray(x, dtype=float)
    return 100.0 * x.std(ddof=1) / x.mean()


def summary(runs, cols, by="case_id"):
    """Per group: n, and mean, SD and CV in % of every column (wide table)."""
    g = runs.groupby(by)
    out = pd.DataFrame({"n": g.size()})
    for c in cols:
        out[f"{c}_mean"] = g[c].mean()
        out[f"{c}_sd"] = g[c].std(ddof=1)
        out[f"{c}_cv_pct"] = 100.0 * out[f"{c}_sd"] / out[f"{c}_mean"]
    return out.reset_index()


def welch(ref, other, conf=CONF):
    """Welch t-test of `other` against `ref`, two-sided.

    diff = mean(other) - mean(ref), with its Welch confidence interval. The %
    values divide by the reference mean and treat it as fixed (with a CV of a
    few % this changes the interval only slightly). If neither group has any
    spread, t, df, p and the interval are NaN.
    """
    a = np.asarray(ref, dtype=float)
    b = np.asarray(other, dtype=float)
    na, nb = len(a), len(b)
    ma, mb = a.mean(), b.mean()
    va, vb = a.var(ddof=1), b.var(ddof=1)
    diff = mb - ma
    se2 = va / na + vb / nb
    if se2 > 0:
        se = math.sqrt(se2)
        t = diff / se
        df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
        p = 2.0 * sp_stats.t.sf(abs(t), df)
        half = sp_stats.t.ppf(0.5 + conf / 2.0, df) * se
        lo, hi = diff - half, diff + half
    else:
        t = df = p = lo = hi = math.nan
    return {
        "n_ref": na, "n_other": nb,
        "mean_ref": ma, "sd_ref": math.sqrt(va), "mean_other": mb, "sd_other": math.sqrt(vb),
        "diff": diff, "diff_lo": lo, "diff_hi": hi,
        "diff_pct": 100.0 * diff / ma, "diff_pct_lo": 100.0 * lo / ma, "diff_pct_hi": 100.0 * hi / ma,
        "t": t, "df": df, "p_welch": p,
    }


def mann_whitney(ref, other):
    """Exact two-sided Mann-Whitney U p-value (the check test)."""
    r = sp_stats.mannwhitneyu(np.asarray(other, float), np.asarray(ref, float),
                              alternative="two-sided", method="exact")
    return float(r.pvalue)


def holm(pvalues):
    """Holm-adjusted p-values in input order. NaN stays NaN and is not counted."""
    p = np.asarray(pvalues, dtype=float)
    out = np.full(p.shape, math.nan)
    idx = np.flatnonzero(~np.isnan(p))
    m = len(idx)
    running = 0.0
    for k, i in enumerate(idx[np.argsort(p[idx], kind="stable")]):
        running = max(running, min(1.0, (m - k) * p[i]))
        out[i] = running
    return out


def compare_values(family, comparison, metric, ref_name, ref, other_name, other):
    """One test row: Welch (main) and exact Mann-Whitney (check)."""
    row = {"family": family, "comparison": comparison, "metric": metric,
           "ref": ref_name, "other": other_name}
    row.update(welch(ref, other))
    row["p_mw"] = mann_whitney(ref, other)
    return row


def compare_cases(runs, family, comparison, ref_case, other_case, metrics):
    """Test rows for two cases, one per metric (column of runs)."""
    return [compare_values(family, comparison, m, ref_case, values(runs, ref_case, m),
                           other_case, values(runs, other_case, m)) for m in metrics]


def finish_tests(rows):
    """Table of test rows with Holm per family and the decision at ALPHA."""
    df = pd.DataFrame(rows)
    df["m_family"] = df.groupby("family")["p_welch"].transform(lambda s: s.notna().sum())
    df["p_holm"] = df.groupby("family")["p_welch"].transform(lambda s: holm(s.to_numpy()))
    df["holm_ok"] = df["p_holm"] < ALPHA
    return df


LOGICAL_CPUS = 8


def perf_seconds(perf):
    """perf_1s (long format) -> one row per run and interval: dt, clock while busy (MHz),
    busy CPUs, IPC, DRAM traffic (GiB/s) and package power (W). Same formula as
    benchlib.py: TSC rate = msr/tsc / 8 CPUs / interval; the clock only where the CPUs
    were busy for more than 0.1 CPU. t_s is the end of the interval, counted from perf's
    start (a few ms before the window start)."""
    w = perf.pivot_table(index=["run_id", "t_s"], columns="event", values="value", aggfunc="first").reset_index()
    w = w.sort_values(["run_id", "t_s"])
    w["dt"] = w.groupby("run_id")["t_s"].diff().fillna(w["t_s"])
    tsc_hz = w["msr/tsc/"] / LOGICAL_CPUS / w["dt"]
    busy_ok = w["ref-cycles"] > 0.1 * tsc_hz * w["dt"]
    return pd.DataFrame({
        "run_id": w["run_id"], "t_s": w["t_s"], "dt": w["dt"],
        "busy_cpus": w["ref-cycles"] / tsc_hz / w["dt"],
        "clock_mhz": (w["cycles"] / w["ref-cycles"] * tsc_hz / 1e6).where(busy_ok),
        "ipc": w["instructions"] / w["cycles"],
        "dram_gib_s": (w["uncore_imc/data_reads/"] + w["uncore_imc/data_writes/"]) / 1024 / w["dt"],
        "pkg_w": w["power/energy-pkg/"] / w["dt"],
    })


def dram_model(runs):
    """Least-squares fit DRAM energy = base_w x duration + j_per_gib x DRAM traffic
    (uncore_imc reads + writes), over the logged runs except idle. Returns the two
    coefficients, R^2 and the runs with the model value and the error in %."""
    x = runs[runs["dram_read_gib"].notna() & (runs["case_id"] != "idle")].copy()
    x["dram_gib"] = x["dram_read_gib"] + x["dram_write_gib"]
    a = np.c_[x["duration_s"], x["dram_gib"]]
    (base_w, j_per_gib), *_ = np.linalg.lstsq(a, x["dram_energy_j"].to_numpy(), rcond=None)
    x["dram_model_j"] = a @ np.array([base_w, j_per_gib])
    x["dram_model_err_pct"] = 100 * (x["dram_energy_j"] / x["dram_model_j"] - 1)
    ss = ((x["dram_energy_j"] - x["dram_model_j"]) ** 2).sum()
    st = ((x["dram_energy_j"] - x["dram_energy_j"].mean()) ** 2).sum()
    return {"base_w": base_w, "j_per_gib": j_per_gib, "r2": 1 - ss / st, "n": len(x)}, x


def write_csv(df, name, out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.csv"
    df.to_csv(path, index=False, float_format="%.6g")
    print("wrote", path)
    return path


# ---- plot style -------------------------------------------------------------
# Colours from the validated reference palette of the dataviz guide (light mode,
# checked with its validate_palette.js on 2026-10-31). A colour always means the
# same thing in every figure.

INK = "#0b0b0b"         # text
INK2 = "#52514e"        # tick labels, error bars
MUTED = "#898781"       # idle part, reference lines
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#ffffff"     # the page is white in print

# the three jobs (categorical slots 1-3, pass all pairs)
JOB = {"cpu": "#2a78d6", "ram": "#eb6834", "io": "#1baf7a"}
# RQ2 bars (slots violet, magenta, green; pass all pairs)
COND = {"sum": "#6250d6", "seq": "#e87ba4", "par": "#008300"}
# ordered levels: one blue, light -> dark (ordinal check passed)
THREADS = {1: "#86b6ef", 2: "#3987e5", 4: "#1c5cab", 8: "#0d366b"}
LOAD = {25: "#86b6ef", 50: "#2a78d6", 100: "#104281"}
# parallel = solid line, filled marker; sequential = dashed line, open marker
MODE_LINE = {"parallel": "-", "sequential": "--"}
IDLE_COLOR = MUTED
IDLE_HATCH = "////"

FULL_W = 6.2            # inches, about \textwidth of the thesis (A4, 12 pt)
HALF_W = 3.0

ERRORBAR = {"ecolor": INK2, "elinewidth": 0.8, "capsize": 2.5, "capthick": 0.8}

STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "text.color": INK,
    "axes.labelcolor": INK,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.grid.axis": "y",
    "axes.axisbelow": True,
    "grid.color": GRID,
    "grid.linewidth": 0.5,
    "lines.linewidth": 1.5,
    "lines.markersize": 5.5,
    "legend.frameon": False,
    "hatch.linewidth": 0.6,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
    "pdf.fonttype": 42,
}


def setup_style():
    plt.rcParams.update(STYLE)


def save_figure(fig, name, figures=FIGURES, preview=PREVIEW):
    """thesis/figures/<name>.pdf for the thesis, analysis/preview/<name>.png to look at."""
    for folder, ext, kw in ((Path(figures), "pdf", {}), (Path(preview), "png", {"dpi": 150})):
        folder.mkdir(parents=True, exist_ok=True)
        fig.savefig(folder / f"{name}.{ext}", **kw)
    plt.close(fig)
    print("wrote", Path(figures) / f"{name}.pdf")
