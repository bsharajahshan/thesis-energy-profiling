#!/usr/bin/env python3

"""
diagnostics_summary.py - summarize the diagnose_work.sh side-run

Reads diagnostics.csv and work_log.csv from the diagnostics folder and prints:
1. per case: package/DRAM power, CPU utilisation, CPU frequency/temperature
2. what the memory worker (vm) and CPU workers actually did: operations per
   second and CPU share per worker (the evidence for/against the cpu_ram
   hypotheses: H1 scheduling/SMT, H2 power limit)
3. DRAM energy per unit of memory work (J per million vm bogo-ops)
4. DRAM traffic (MiB read/written, from perf uncore_imc counters if the
   machine has them; otherwise an estimate from vm bogo-ops x chunk size) -
   Philipp's request: does the combined run move the same amount of data?
5. hyperthreading check: 8 threads vs 4 threads at 100%, and 8 threads at
   50% load (--cpu-load 50) - does the second hardware thread add work?
6. logging-overhead check: same workload with and without logging

Usage: py scripts/analysis/diagnostics_summary.py [results/diagnostics]
Nothing here touches the main dataset in results/raw.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def main(folder: str) -> int:
    base = Path(folder)
    diag_path, work_path = base / "diagnostics.csv", base / "work_log.csv"
    if not diag_path.exists():
        print(f"No diagnostics.csv in {base}")
        return 1

    diag = pd.read_csv(diag_path)
    for col in ["duration_s", "energy_j", "dram_energy_j", "cpu_util_pct",
                "avg_bzy_mhz", "max_pkg_temp_c", "avg_pkg_watt_turbostat"]:
        diag[col] = pd.to_numeric(diag[col], errors="coerce")
    diag["pkg_w"] = diag["energy_j"] / diag["duration_s"]
    diag["dram_w"] = diag["dram_energy_j"] / diag["duration_s"]

    print("=== 1. Per case (mean over repetitions) ===")
    per_case = diag.groupby("case").agg(
        n=("run_id", "count"), pkg_w=("pkg_w", "mean"), dram_w=("dram_w", "mean"),
        dram_w_sd=("dram_w", "std"), cpu_util_pct=("cpu_util_pct", "mean"),
        bzy_mhz=("avg_bzy_mhz", "mean"), max_temp_c=("max_pkg_temp_c", "max"),
    ).round(2)
    print(per_case.to_string())

    if work_path.exists():
        work = pd.read_csv(work_path)
        for col in ["bogo_ops", "real_s", "usr_s", "sys_s", "bogo_ops_per_s", "cpu_share_pct"]:
            work[col] = pd.to_numeric(work[col], errors="coerce")

        print("\n=== 2. What each worker did (mean over repetitions) ===")
        print("(io_fio row: bogo_ops = bytes written, real_s = runtime in ms, usr_s/sys_s = fio CPU %,"
              " bogo_ops_per_s = IOPS)")
        per_worker = work.groupby(["case", "component"]).agg(
            workers=("workers", "first"), ops_per_s=("bogo_ops_per_s", "mean"),
            ops_per_s_sd=("bogo_ops_per_s", "std"), cpu_share_pct=("cpu_share_pct", "mean"),
        ).round(1)
        print(per_worker.to_string())

        vm = work[work["component"] == "vm"].merge(
            diag[["case", "run_id", "dram_energy_j", "duration_s"]], on=["case", "run_id"])
        if not vm.empty:
            vm["dram_j_per_mops"] = vm["dram_energy_j"] / (vm["bogo_ops"] / 1e6)
            print("\n=== 3. Memory worker: speed vs DRAM energy ===")
            print(vm.groupby("case").agg(
                vm_ops_per_s=("bogo_ops_per_s", "mean"), vm_cpu_share_pct=("cpu_share_pct", "mean"),
                dram_j=("dram_energy_j", "mean"), dram_j_per_million_ops=("dram_j_per_mops", "mean"),
            ).round(2).to_string())
            print("Reading it: if cpu_ram's vm ops/s and CPU share are clearly lower than ram_alone and "
                  "cpu4_ram is back near ram_alone, that supports H1 (scheduling). If the vm worker's speed is "
                  "unchanged but frequency/PkgWatt show the chip pinned at its limit, look at H2 (power limit).")

        print("\n=== 4. DRAM traffic per case ===")
        perf_path = base / "perf_log.csv"
        traffic = None
        if perf_path.exists():
            perf = pd.read_csv(perf_path)
            perf["value"] = pd.to_numeric(perf["value"], errors="coerce")
            imc = perf[perf["event"].astype(str).str.contains("uncore_imc", na=False)].copy()
            if not imc.empty:
                imc["kind"] = np.where(imc["event"].str.contains("read"), "read_mib", "write_mib")
                per_run = imc.pivot_table(index=["case", "run_id"], columns="kind", values="value",
                                          aggfunc="sum").reset_index()
                per_run = per_run.merge(diag[["case", "run_id", "duration_s", "dram_energy_j"]],
                                        on=["case", "run_id"])
                per_run["total_gib"] = (per_run.get("read_mib", 0) + per_run.get("write_mib", 0)) / 1024
                per_run["gib_per_s"] = per_run["total_gib"] / per_run["duration_s"]
                per_run["dram_j_per_gib"] = per_run["dram_energy_j"] / per_run["total_gib"]
                traffic = per_run.groupby("case")[
                    [c for c in ["read_mib", "write_mib", "total_gib", "gib_per_s", "dram_j_per_gib"]
                     if c in per_run.columns]].mean().round(2)
                print("Measured with perf uncore_imc (system-wide, all processes):")
                print(traffic.to_string())
        if traffic is None:
            # Fallback: one vm bogo-op = one pass over the worker's chunk (8 GB here),
            # same convention as verify_work_equivalence.sh. Rough estimate of data
            # the memory worker touched, not measured DRAM traffic.
            if not vm.empty:
                vm["gb_touched_est"] = vm["bogo_ops"] * 8
                print("No uncore_imc counters found - ESTIMATE from vm bogo-ops x 8 GB chunk "
                      "(memory worker only, not measured traffic):")
                print(vm.groupby("case")[["gb_touched_est", "dram_energy_j"]].mean().round(1).to_string())
        print("Reading it: if cpu_ram moves clearly less data than ram_alone/ram_io, the lower DRAM "
              "energy is explained by less memory work, not by cheaper memory work.")

        print("\n=== 5. Hyperthreading check (CPU only) ===")
        ht_cases = ["cpu4_alone", "cpu_alone", "cpu8_half"]
        cpu_work = work[(work["case"].isin(ht_cases)) & (work["component"].str.startswith("cpu"))]
        if not cpu_work.empty:
            ht = cpu_work.merge(diag[["case", "run_id", "energy_j", "pkg_w"]], on=["case", "run_id"])
            ht["j_per_million_ops"] = ht["energy_j"] / (ht["bogo_ops"] / 1e6)
            table = ht.groupby("case").agg(
                threads=("workers", "first"), total_ops_per_s=("bogo_ops_per_s", "mean"),
                cpu_share_pct=("cpu_share_pct", "mean"), pkg_w=("pkg_w", "mean"),
                j_per_million_ops=("j_per_million_ops", "mean"),
            ).reindex([c for c in ht_cases if c in set(ht["case"])]).round(2)
            print(table.to_string())
            print("Reading it: cpu_alone (8 threads at 100%) vs cpu4_alone (4 threads at 100%): if total "
                  "ops/s is about the same, the second hardware thread adds no work (Philipp's expectation "
                  "for stress-ng). cpu8_half should show cpu_share near 50%; compare its ops/s and J per "
                  "million ops with cpu4_alone (same nominal work).")

    print("\n=== 6. Logging overhead check (same workload, with vs. without logging) ===")
    for logged, plain in [("ram_alone", "ram_alone_nolog"), ("cpu_ram", "cpu_ram_nolog")]:
        a = diag[diag["case"] == logged]
        b = diag[diag["case"] == plain]
        if a.empty or b.empty:
            continue
        row = f"{logged} vs {plain}: "
        for col, label in [("energy_j", "package J"), ("dram_energy_j", "DRAM J")]:
            ma, mb = a[col].mean(), b[col].mean()
            p = np.nan
            if len(a) > 1 and len(b) > 1 and (a[col].nunique() > 1 or b[col].nunique() > 1):
                p = sp_stats.mannwhitneyu(a[col], b[col], alternative="two-sided").pvalue
            row += f"{label} {ma:.1f} vs {mb:.1f} ({(ma - mb) / mb * 100:+.1f}%, p={p:.3f}, n={len(a)}/{len(b)})   "
        print(row)
    print("Logging is harmless if these differences are within the normal run-to-run spread "
          "(about 3% for package, more for DRAM) and not significant.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "results/diagnostics"))
