#!/usr/bin/env python3

"""
govcheck.py - the governor check, the second session on the laptop (analysis piece 7)

Three cases of the main session, each with three power settings, 5 rounds:
  default   governor `powersave`, EPP `balance_performance` (Ubuntu's default; the case itself)
  governor  governor `performance`; the kernel then also sets EPP `performance` (gov_*)
  epp       governor `powersave`, EPP `performance` (epp_*)
Cases: rq3_cpu_load50_n1 and rq3_cpu_load50_n4 (phases at about 1.2 GHz in the main session)
and rq2_cpu_ram_par (about 2.4 GHz, below the power limit). Question: do the default
settings cause these low clocks? Every comparison stays inside this session, with its own
idle reference (its 10 idle runs).

Reads results/rerun_2026-10-31/govcheck/tables/ (extract_rerun.py on thesis_govcheck.tar.gz).
Writes to results/rerun_2026-10-31/analysis/:
  govcheck_settings.csv   per setting: the expected governor and EPP, runs, runs that had them
                          at the start and at the end of the run
  govcheck_summary.csv    per case and setting (and idle): mean, SD, CV of package/DRAM energy
                          (total, above idle), time, power, clock, seconds at a low clock,
                          temperature, throttling
  govcheck_low_clock.csv  every run except idle: seconds at a low clock (< 1.5 GHz, as in rq3.py),
                          clock and package power in the low and the high seconds
  govcheck_tests.csv      Welch + Holm (family govcheck) and exact Mann-Whitney, for package energy,
                          DRAM energy and time: per case governor vs default, EPP vs default,
                          governor vs EPP
Figure (thesis/figures/):
  govcheck.pdf            per case and setting, every run: package energy, time, seconds at a
                          low clock

Usage: py scripts/analysis/rerun/govcheck.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

TABLES = c.DATA / "govcheck" / "tables"
BASE = ["rq3_cpu_load50_n1", "rq3_cpu_load50_n4", "rq2_cpu_ram_par"]
SETTINGS = ["default", "governor", "epp"]
PREFIX = {"default": "", "governor": "gov_", "epp": "epp_"}
EXPECTED = {"default": ("powersave", "balance_performance"), "governor": ("performance", "performance"),
            "epp": ("powersave", "performance")}
METRICS = ["pkg_energy_j", "dram_energy_j", "duration_s"]
LOW_CLOCK_MHZ = 1500    # the same limit as rq3.py
BASE_NAME = {"rq3_cpu_load50_n1": "CPU 50 % load, N=1", "rq3_cpu_load50_n4": "CPU 50 % load, N=4",
             "rq2_cpu_ram_par": "CPU + memory, parallel"}
SETTING_TICK = {"default": "default", "governor": "governor\nperf.", "epp": "EPP\nperf."}


def case_id(base, setting):
    return PREFIX[setting] + base


def label_runs(runs):
    """Add base (the main-session case) and setting to every run; idle is base idle, default."""
    out = runs.copy()
    out["setting"] = out["case_id"].str.extract(r"^(gov|epp)_")[0].map({"gov": "governor", "epp": "epp"})
    out["setting"] = out["setting"].fillna("default")
    out["base"] = out["case_id"].str.replace(r"^(gov|epp)_", "", regex=True)
    return out


def settings(r):
    rows = []
    for s in SETTINGS:
        g = r[r["setting"] == s]
        gov, epp = EXPECTED[s]
        ok = ((g["governor_start"] == gov) & (g["governor_end"] == gov)
              & (g["epp_start"] == epp) & (g["epp_end"] == epp))
        rows.append({"setting": s, "governor_expected": gov, "epp_expected": epp, "n_runs": len(g),
                     "n_as_expected": int(ok.sum()),
                     "governor_seen": " ".join(sorted(set(g["governor_start"]) | set(g["governor_end"]))),
                     "epp_seen": " ".join(sorted(set(g["epp_start"]) | set(g["epp_end"])))})
    return pd.DataFrame(rows)


def low_clock(r, persec):
    x = persec.merge(r[["run_id", "case_id", "base", "setting", "rep", "duration_s", "pkg_energy_j"]], on="run_id")
    x = x[x["base"].isin(BASE)]
    x["low"] = x["clock_mhz"] < LOW_CLOCK_MHZ
    g = x.groupby("run_id")
    out = pd.DataFrame({
        "case_id": g["case_id"].first(), "base": g["base"].first(), "setting": g["setting"].first(),
        "rep": g["rep"].first(),
        "low_clock_s": g.apply(lambda d: (d["dt"] * d["low"]).sum()),
        "clock_high_mhz": g.apply(lambda d: d.loc[~d["low"] & d["clock_mhz"].notna(), "clock_mhz"].mean()),
        "clock_low_mhz": g.apply(lambda d: d.loc[d["low"], "clock_mhz"].mean()),
        "pkg_w_low": g.apply(lambda d: d.loc[d["low"], "pkg_w"].mean()),
        "pkg_w_high": g.apply(lambda d: d.loc[~d["low"], "pkg_w"].mean()),
        "duration_s": g["duration_s"].first(), "pkg_energy_j": g["pkg_energy_j"].first(),
    }).reset_index()
    return sort_rows(out, ["rep"])


def sort_rows(df, extra=()):
    key = {"base": {b: i for i, b in enumerate(["idle"] + BASE)}, "setting": {s: i for i, s in enumerate(SETTINGS)}}
    return df.sort_values(["base", "setting", *extra], key=lambda col: col.map(key[col.name]) if col.name in key else col)


SUMMARY_COLS = ["pkg_energy_j", "pkg_active_j", "pkg_idle_j", "dram_energy_j", "dram_active_j", "duration_s",
                "pkg_power_w", "busy_mhz", "low_clock_s", "clock_high_mhz", "temp_max_c", "pkg_power_max_1s_w",
                "thr_core_ms", "thr_pkg_ms"]


def summary(r):
    s = c.summary(r, SUMMARY_COLS, by=["base", "setting"])
    return sort_rows(s)


def tests(runs):
    rows = []
    for b in BASE:
        for ref, other in (("default", "governor"), ("default", "epp"), ("epp", "governor")):
            rows += c.compare_cases(runs, "govcheck", f"{b}: {other} vs {ref}", case_id(b, ref), case_id(b, other),
                                    METRICS)
    return c.finish_tests(rows)


# ---- figure -------------------------------------------------------------------

def fig_govcheck(r):
    rows = (("pkg_energy_j", "Package energy (J)"), ("duration_s", "Time (s)"),
            ("low_clock_s", "Seconds below 1.5 GHz"))
    fig, axes = c.plt.subplots(len(rows), len(BASE), figsize=(c.FULL_W, 5.6), sharex=True, sharey="row")
    offsets = [-0.12, -0.06, 0.0, 0.06, 0.12]    # by round, so the five runs do not cover each other
    for j, b in enumerate(BASE):
        for i, (col, label) in enumerate(rows):
            ax = axes[i, j]
            for k, s in enumerate(SETTINGS):
                d = r[(r["base"] == b) & (r["setting"] == s)].sort_values("rep")
                ax.scatter([k + o for o in offsets[:len(d)]], d[col], s=16, color=c.JOB["cpu"],
                           edgecolor=c.SURFACE, linewidth=0.5, zorder=3)
                ax.plot([k - 0.22, k + 0.22], [d[col].mean()] * 2, color=c.INK, linewidth=1.2, zorder=2)
            if j == 0:
                ax.set_ylabel(label)
        axes[0, j].set_title(BASE_NAME[b], fontsize=9)
        axes[-1, j].set_xticks(range(len(SETTINGS)), [SETTING_TICK[s] for s in SETTINGS])
        axes[-1, j].set_xlim(-0.5, len(SETTINGS) - 0.5)
    axes[-1, 0].set_ylim(-2, None)
    fig.tight_layout()
    return fig


def main():
    runs = label_runs(c.load("runs", TABLES))
    idle = c.idle_reference(runs)
    print(f"idle reference of this session: package {idle['pkg_w']:.3f} W, DRAM {idle['dram_w']:.3f} W "
          f"({idle['n']} runs)")
    r = c.add_active(runs, idle)

    st = settings(r)
    c.write_csv(st, "govcheck_settings")
    print(f"runs with the expected governor and EPP at start and end: {st['n_as_expected'].sum()} "
          f"of {st['n_runs'].sum()}")

    ids = r.loc[r["base"].isin(BASE), "run_id"]
    perf = c.load("perf_1s", TABLES)
    persec = c.perf_seconds(perf[perf["run_id"].isin(ids)])
    low = low_clock(r, persec)
    c.write_csv(low, "govcheck_low_clock")
    r = r.merge(low[["run_id", "low_clock_s", "clock_high_mhz"]], on="run_id", how="left")

    c.write_csv(summary(r), "govcheck_summary")
    c.write_csv(tests(runs), "govcheck_tests")

    c.setup_style()
    c.save_figure(fig_govcheck(r[r["base"].isin(BASE)]), "govcheck")


if __name__ == "__main__":
    main()
