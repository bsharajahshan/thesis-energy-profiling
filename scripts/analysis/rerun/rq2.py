#!/usr/bin/env python3

"""
rq2.py - RQ2: jobs together (parallel) or one after another (sequential) (analysis piece 3)

Cases: rq2_{cpu_ram, cpu_io, ram_io, cpu_ram_io}_{par,seq}, built from the max-level
RQ1 jobs: CPU 8 threads (cpu_t8), memory 8 GB (mem_8g), sequential I/O (io_seq).
Three conditions per combination:
  sum  the isolated RQ1 runs of the jobs, added per round (5 sums)
  seq  the jobs one after another in one window
  par  the jobs at the same time in one window
The idle part (idle power x time) is shown and also taken out ("active" energy),
because the sum and the sequential run contain more idle time than the parallel run.

Writes to results/rerun_2026-10-31/analysis/:
  rq2_summary.csv     per combination and condition: mean, SD, CV of package/DRAM energy
                      (total, above idle, idle part), time, power, clock, DRAM traffic
  rq2_tests.csv       Welch + Holm (family rq2) and exact Mann-Whitney: par vs seq, par vs sum,
                      seq vs sum, for package and DRAM energy (total and above idle) and time
  rq2_parts.csv       every job in every RQ2 run: own time, speed and (sequential) own energy,
                      against the same job alone
  rq2_parts_summary.csv  the same per combination, condition and job (sequential also per position)
  rq2_temp_power_fit.csv memory job: package power against its mean temperature (alone and as a
                      sequential part), slope in W per C; once with only the parts at the job's
                      usual clock (same work and clock, only the temperature differs), once all
  rq2_marginal.csv    what a job adds when it joins the others in a parallel run, against
                      what it costs alone (difference of means with Welch CI)
  dram_model.csv      DRAM energy = base power x time + energy per GiB, fitted over all logged
                      runs; mean error per case (also used for RQ3)
Figures (thesis/figures/):
  rq2_energy.pdf      package and DRAM energy per condition, idle part hatched
  rq2_time.pdf        time per condition, with the end of each job in the parallel run

Usage: py scripts/analysis/rerun/rq2.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c  # noqa: E402

COMBOS = {"cpu_ram": ["cpu", "ram"], "cpu_io": ["cpu", "io"], "ram_io": ["ram", "io"],
          "cpu_ram_io": ["cpu", "ram", "io"]}
LABEL = {"cpu_ram": "CPU + memory", "cpu_io": "CPU + I/O", "ram_io": "Memory + I/O",
         "cpu_ram_io": "CPU + memory + I/O"}
ISO = {"cpu": "cpu_t8", "ram": "mem_8g", "io": "io_seq"}
JOB_NAME = {"cpu": "CPU", "ram": "memory", "io": "I/O"}
CONDS = ["sum", "seq", "par"]
COND_NAME = {"sum": "Sum of isolated jobs", "seq": "Sequential", "par": "Parallel"}
METRICS = ["pkg_energy_j", "pkg_active_j", "dram_energy_j", "dram_active_j", "duration_s"]
EXTRA = ["pkg_idle_j", "dram_idle_j", "dram_gib"]


def conditions(runs, idle):
    """Long table: combination, condition, rep and every metric (sum built per round)."""
    r = c.add_active(runs, idle)
    r["dram_gib"] = r["dram_read_gib"] + r["dram_write_gib"]
    rows = []
    for combo, jobs in COMBOS.items():
        for cond in ("seq", "par"):
            sel = r[r["case_id"] == f"rq2_{combo}_{cond}"]
            for _, run in sel.iterrows():
                rows.append({"combo": combo, "cond": cond, "rep": run["rep"],
                             **{m: run[m] for m in METRICS + EXTRA + ["busy_mhz", "busy_cpus"]}})
        sums = {m: sum(c.by_rep(r, ISO[j], m) for j in jobs) for m in METRICS + EXTRA}
        for rep in sums["pkg_energy_j"].index:
            rows.append({"combo": combo, "cond": "sum", "rep": rep, **{m: sums[m].loc[rep] for m in sums}})
    out = pd.DataFrame(rows)
    out["pkg_power_w"] = out["pkg_energy_j"] / out["duration_s"]
    return out


def summary(cond):
    g = cond.groupby(["combo", "cond"])
    out = pd.DataFrame({"n": g.size()})
    for m in METRICS + EXTRA + ["pkg_power_w", "busy_mhz", "busy_cpus"]:
        out[f"{m}_mean"] = g[m].mean()
        out[f"{m}_sd"] = g[m].std(ddof=1)
        out[f"{m}_cv_pct"] = 100 * out[f"{m}_sd"] / out[f"{m}_mean"]
    out = out.reset_index()
    out["combo_order"] = out["combo"].map({k: i for i, k in enumerate(COMBOS)})
    out["cond_order"] = out["cond"].map({k: i for i, k in enumerate(CONDS)})
    return out.sort_values(["combo_order", "cond_order"]).drop(columns=["combo_order", "cond_order"])


def tests(cond):
    rows = []
    for combo in COMBOS:
        for ref, other in (("seq", "par"), ("sum", "par"), ("sum", "seq")):
            a = cond[(cond["combo"] == combo) & (cond["cond"] == ref)]
            b = cond[(cond["combo"] == combo) & (cond["cond"] == other)]
            for m in METRICS:
                rows.append(c.compare_values("rq2", f"{combo}: {other} vs {ref}", m, ref, a[m], other, b[m]))
    return c.finish_tests(rows)


SAME_CLOCK_PCT = 1.0     # a part counts as "at the job's usual clock" within 1 % of its alone clock


def part_conditions(parts_tbl, runs, samples, persec, cases):
    """Per part, over the seconds inside its own time (first second left out): mean package
    temperature (sampler) and clock, DRAM traffic, IPC (perf)."""
    start = runs.set_index("run_id")["t_start_us"]
    rows = []
    for _, p in parts_tbl[parts_tbl["case_id"].isin(cases)].iterrows():
        a = (p["t_start_us"] - start[p["run_id"]]) / 1e6 + 1
        b = (p["t_end_us"] - start[p["run_id"]]) / 1e6
        s = samples[(samples["run_id"] == p["run_id"]) & (samples["t_s"] > a) & (samples["t_s"] <= b)]
        k = persec[(persec["run_id"] == p["run_id"]) & (persec["t_s"] - persec["dt"] >= a) & (persec["t_s"] <= b)]
        rows.append({"run_id": p["run_id"], "part_index": p["part_index"],
                     "temp_first_c": s["temp_c"].iloc[0] if len(s) else np.nan, "temp_mean_c": s["temp_c"].mean(),
                     "clock_mhz": k["clock_mhz"].mean(), "dram_gib_s": k["dram_gib_s"].mean(), "ipc": k["ipc"].mean()})
    return pd.DataFrame(rows)


def temp_power_fit(pt, runs, parts_tbl, cond_parts):
    """Memory job: package power against its mean temperature, alone (mem_8g) and as a part of a
    sequential RQ2 run. Only parts at the usual clock of the job alone (within SAME_CLOCK_PCT), so
    work and clock are the same and only the temperature differs. Slope in W per C."""
    from scipy import stats as sp_stats
    alone = (parts_tbl[parts_tbl["case_id"] == ISO["ram"]].merge(cond_parts, on=["run_id", "part_index"])
             .merge(runs[["run_id", "pkg_power_w"]], on="run_id").rename(columns={"pkg_power_w": "power"}))
    usual = alone["clock_mhz"].mean()
    seq = pt[(pt["cond"] == "seq") & (pt["job"] == "ram")]
    x = pd.concat([alone[["temp_mean_c", "power", "clock_mhz"]].assign(where="alone"),
                   seq.assign(power=seq["pkg_energy_j"] / seq["duration_s"])[["temp_mean_c", "power", "clock_mhz"]]
                   .assign(where="sequential part")]).dropna()
    x["same_clock"] = (100 * (x["clock_mhz"] / usual - 1)).abs() < SAME_CLOCK_PCT
    rows = []
    for label, sel in (("same clock", x[x["same_clock"]]), ("all", x)):
        lr = sp_stats.linregress(sel["temp_mean_c"], sel["power"])
        half = sp_stats.t.ppf(0.975, len(sel) - 2) * lr.stderr
        rows.append({"job": "ram", "parts": label, "usual_clock_mhz": usual, "n": len(sel),
                     "n_alone": int((sel["where"] == "alone").sum()), "slope_w_per_c": lr.slope,
                     "slope_lo": lr.slope - half, "slope_hi": lr.slope + half, "intercept_w": lr.intercept,
                     "r": lr.rvalue, "temp_min": sel["temp_mean_c"].min(), "temp_max": sel["temp_mean_c"].max(),
                     "power_min": sel["power"].min(), "power_max": sel["power"].max(),
                     "n_faster_clock": int((~x["same_clock"]).sum()),
                     "faster_clock_mhz": x.loc[~x["same_clock"], "clock_mhz"].mean()})
    return pd.DataFrame(rows)


def parts(runs, parts_tbl, cond_parts):
    """Every job of every RQ2 run, against the same job alone (mean of its 5 isolated runs)."""
    iso = {}
    for job, case in ISO.items():
        p = parts_tbl[parts_tbl["case_id"] == case]
        r = runs[runs["case_id"] == case]
        iso[job] = {"duration_s": p["duration_s"].mean(), "ops_per_s": p["ops_per_s"].mean(),
                    "fio_iops": p["fio_iops"].mean(), "pkg_energy_j": r["pkg_energy_j"].mean(),
                    "dram_energy_j": r["dram_energy_j"].mean()}
    rows = []
    sel = parts_tbl[parts_tbl["case_id"].str.match(r"rq2_.*_(par|seq)$")].merge(cond_parts, on=["run_id", "part_index"])
    for run_id, g in sel.groupby("run_id"):
        case = g["case_id"].iloc[0]
        combo, cond = case[4:].rsplit("_", 1)
        g = g.sort_values("t_start_us")
        t0 = g["t_start_us"].min()
        for pos, (_, p) in enumerate(g.iterrows(), start=1):
            job = p["part_name"]
            ref = iso[job]
            speed = p["fio_iops"] if job == "io" else p["ops_per_s"]
            ref_speed = ref["fio_iops"] if job == "io" else ref["ops_per_s"]
            row = {"run_id": run_id, "combo": combo, "cond": cond, "job": job,
                   "position": pos if cond == "seq" else np.nan,
                   "start_s": (p["t_start_us"] - t0) / 1e6, "end_s": (p["t_end_us"] - t0) / 1e6,
                   "duration_s": p["duration_s"], "duration_ratio": p["duration_s"] / ref["duration_s"],
                   "speed": speed, "speed_ratio": speed / ref_speed,
                   "pkg_energy_j": p["sub_pkg_energy_j"], "dram_energy_j": p["sub_dram_energy_j"],
                   "temp_first_c": p["temp_first_c"], "temp_mean_c": p["temp_mean_c"],
                   "clock_mhz": p["clock_mhz"], "dram_gib_s": p["dram_gib_s"], "ipc": p["ipc"]}
            if cond == "seq":
                row["pkg_energy_ratio"] = p["sub_pkg_energy_j"] / ref["pkg_energy_j"]
                row["dram_energy_ratio"] = p["sub_dram_energy_j"] / ref["dram_energy_j"]
            rows.append(row)
    return pd.DataFrame(rows)


def parts_summary(pt):
    cols = ["start_s", "end_s", "duration_s", "duration_ratio", "speed_ratio", "pkg_energy_j",
            "pkg_energy_ratio", "dram_energy_ratio", "temp_first_c", "temp_mean_c", "clock_mhz", "dram_gib_s"]
    out = []
    for keys in (["combo", "cond", "job"], ["cond", "job", "position"]):
        sel = pt if "position" not in keys else pt[pt["cond"] == "seq"]
        g = sel.groupby(keys)
        s = pd.DataFrame({"n": g.size()})
        for col in cols:
            s[f"{col}_mean"] = g[col].mean()
            s[f"{col}_sd"] = g[col].std(ddof=1)
        out.append(s.reset_index())
    return pd.concat(out, ignore_index=True)


def marginal(runs, idle):
    """Parallel run with a job minus the same run without it (a single job = its RQ1 run)."""
    r = c.add_active(runs, idle)
    case_of = {tuple(jobs): f"rq2_{combo}_par" for combo, jobs in COMBOS.items()}
    case_of.update({(j,): ISO[j] for j in ISO})
    rows = []
    for combo, jobs in COMBOS.items():
        for job in jobs:
            rest = tuple(j for j in jobs if j != job)
            for m in ("pkg_energy_j", "pkg_active_j", "dram_energy_j", "duration_s"):
                w = c.welch(c.values(r, case_of[rest], m), c.values(r, f"rq2_{combo}_par", m))
                alone = c.values(r, ISO[job], m).mean()
                rows.append({"combo": combo, "job_added": job, "to": case_of[rest], "metric": m,
                             "added": w["diff"], "added_lo": w["diff_lo"], "added_hi": w["diff_hi"],
                             "alone": alone, "added_vs_alone_pct": 100 * w["diff"] / alone})
    return pd.DataFrame(rows)


def dram_model_table(runs):
    fit, x = c.dram_model(runs)
    g = x.groupby("case_id")
    out = pd.DataFrame({"n": g.size(), "dram_energy_j_mean": g["dram_energy_j"].mean(),
                        "dram_model_j_mean": g["dram_model_j"].mean(),
                        "err_pct_mean": g["dram_model_err_pct"].mean(),
                        "dram_gib_mean": g["dram_gib"].mean(), "duration_s_mean": g["duration_s"].mean()})
    out = out.reset_index().sort_values("err_pct_mean")
    for k in ("base_w", "j_per_gib", "r2", "n"):
        out[f"fit_{k}"] = fit[k]
    return out, fit


# ---- figures ------------------------------------------------------------------

def bar_positions():
    width = 0.26
    return width, {cond: (i - 1) * width for i, cond in enumerate(CONDS)}


def fig_energy(s):
    width, dx = bar_positions()
    x = np.arange(len(COMBOS))
    fig, axes = c.plt.subplots(2, 1, figsize=(c.FULL_W, 4.6), sharex=True)
    for ax, dom, label in ((axes[0], "pkg", "Package energy (J)"), (axes[1], "dram", "DRAM energy (J)")):
        for cond in CONDS:
            t = s[s["cond"] == cond].set_index("combo").loc[list(COMBOS)]
            total = t[f"{dom}_energy_j_mean"].to_numpy()
            idle_part = np.minimum(t[f"{dom}_idle_j_mean"].to_numpy(), total)
            ax.bar(x + dx[cond], idle_part, width=width, color=c.GRID, hatch=c.IDLE_HATCH,
                   edgecolor=c.SURFACE, linewidth=1, hatchcolor=c.MUTED)
            ax.bar(x + dx[cond], total - idle_part, bottom=idle_part, width=width, color=c.COND[cond],
                   edgecolor=c.SURFACE, linewidth=1, label=COND_NAME[cond])
            ax.errorbar(x + dx[cond], total, yerr=t[f"{dom}_energy_j_sd"], fmt="none", **c.ERRORBAR)
        ax.set_ylabel(label)
    axes[1].set_xticks(x, [LABEL[k] for k in COMBOS])
    handles, labels = axes[0].get_legend_handles_labels()
    handles.append(c.plt.Rectangle((0, 0), 1, 1, facecolor=c.GRID, hatch=c.IDLE_HATCH, edgecolor=c.MUTED, linewidth=0))
    labels.append("Idle part (idle power × time)")
    fig.legend(handles, labels, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.03), handlelength=1.4)
    fig.tight_layout()
    return fig


def fig_time(s, ps):
    width, dx = bar_positions()
    x = np.arange(len(COMBOS))
    fig, ax = c.plt.subplots(figsize=(c.FULL_W, 2.6))
    for cond in CONDS:
        t = s[s["cond"] == cond].set_index("combo").loc[list(COMBOS)]
        ax.bar(x + dx[cond], t["duration_s_mean"], width=width, color=c.COND[cond], edgecolor=c.SURFACE,
               linewidth=1, label=COND_NAME[cond])
        ax.errorbar(x + dx[cond], t["duration_s_mean"], yerr=t["duration_s_sd"], fmt="none", **c.ERRORBAR)
    ends = ps[(ps["cond"] == "par") & ps["position"].isna() & ps["combo"].notna()]
    for job, marker in (("cpu", "o"), ("ram", "s"), ("io", "D")):
        e = ends[ends["job"] == job].set_index("combo")
        xs = [x[list(COMBOS).index(k)] + dx["par"] for k in e.index]
        ax.scatter(xs, e["end_s_mean"], marker=marker, s=22, color=c.JOB[job], edgecolor=c.SURFACE,
                   linewidth=0.8, zorder=4, label=f"End of the {JOB_NAME[job]} job (parallel)")
    ax.set_xticks(x, [LABEL[k] for k in COMBOS])
    ax.set_ylabel("Time (s)")
    handles, labels = ax.get_legend_handles_labels()
    bars = [i for i, lab in enumerate(labels) if not lab.startswith("End")]
    ends = [i for i, lab in enumerate(labels) if lab.startswith("End")]
    ax.legend([handles[i] for i in bars + ends], [labels[i] for i in bars + ends], loc="upper left", ncol=2,
              fontsize=7)
    ax.set_ylim(0, 215)
    fig.tight_layout()
    return fig


def main():
    runs = c.load("runs")
    parts_tbl = c.load("parts")
    idle = c.idle_reference(runs)

    cond = conditions(runs, idle)
    s = summary(cond)
    c.write_csv(s, "rq2_summary")
    c.write_csv(tests(cond), "rq2_tests")
    cases = [f"rq2_{k}_{m}" for k in COMBOS for m in ("par", "seq")] + list(ISO.values())
    ids = runs.loc[runs["case_id"].isin(cases), "run_id"]
    samples = c.load("samples_1s")
    perf = c.load("perf_1s")
    cond_parts = part_conditions(parts_tbl, runs, samples[samples["run_id"].isin(ids)],
                                 c.perf_seconds(perf[perf["run_id"].isin(ids)]), cases)
    pt = parts(runs, parts_tbl, cond_parts)
    c.write_csv(pt, "rq2_parts")
    c.write_csv(temp_power_fit(pt, runs, parts_tbl, cond_parts), "rq2_temp_power_fit")
    ps = parts_summary(pt)
    c.write_csv(ps, "rq2_parts_summary")
    c.write_csv(marginal(runs, idle), "rq2_marginal")
    dm, fit = dram_model_table(runs)
    c.write_csv(dm, "dram_model")
    print("DRAM model: {base_w:.4f} W + {j_per_gib:.4f} J/GiB, R2 {r2:.4f}, n {n}".format(**fit))

    c.setup_style()
    c.save_figure(fig_energy(s), "rq2_energy")
    c.save_figure(fig_time(s, ps), "rq2_time")


if __name__ == "__main__":
    main()
