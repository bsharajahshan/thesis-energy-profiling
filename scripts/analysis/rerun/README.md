# Analysis of the fixed-work re-run

These scripts turn the tables of the main session (2026-10-30/31, 270 valid
runs) into the numbers, tests and figures of thesis chapters 4 to 6.

## Data

The tables come from the laptop's archive, made once with
[extract_rerun.py](../extract_rerun.py) (from the repo root):

```
python3 -I scripts/analysis/extract_rerun.py results/rerun_2026-10-31 thesis_main.tar.gz thesis_pilot.tar.gz thesis_memcheck.tar.gz thesis_memcheck_rerun.tar.gz
```

This writes `results/rerun_2026-10-31/tables/` (runs, parts, attempts, 1 s
samples, 1 s perf lines, cases) and keeps the unpacked archives in `raw/`.
`rq1.py` reads the fio JSON files from `raw/main/main/runs/`, so keep `raw/`.

## Requirements

Python 3.10 or newer with pandas, scipy and matplotlib. Used for the thesis:
Python 3.14, pandas 3.0.6, scipy 1.18.1, matplotlib 3.11.2 (Windows, `py`).

## Run order

From the repo root:

```
py scripts/analysis/rerun/test_common.py
py scripts/analysis/rerun/quality.py
py scripts/analysis/rerun/rq1.py
py scripts/analysis/rerun/rq2.py
py scripts/analysis/rerun/rq3.py
py scripts/analysis/rerun/mech.py
py scripts/analysis/rerun/controls.py
```

The scripts are independent except one: `controls.py` reads
`rq1_trace_phases.csv`, so run `rq1.py` first.

## What each script does

| Script | Question | Main outputs |
|---|---|---|
| `common.py` | shared helpers: paths, idle reference, active energy, Welch test, Holm, Mann-Whitney, per-second clock from perf, DRAM model, plot style | (none) |
| `test_common.py` | tests of `common.py` (17) | (none) |
| `quality.py` | is the data good? session, spread (CV), work done, throttling, drift, why time spreads | `quality_*.csv`, `quality_clock_time_energy.pdf` |
| `rq1.py` | RQ1: energy of each job alone, CPU thread scaling, throttling traces, I/O | `rq1_*.csv`, `rq1_energy.pdf`, `rq1_cpu_scaling.pdf`, `rq1_traces.pdf` |
| `rq2.py` | RQ2: jobs together vs one after another vs the sum of isolated jobs | `rq2_*.csv`, `dram_model.csv`, `rq2_energy.pdf`, `rq2_time.pdf` |
| `rq3.py` | RQ3: the same work split into N tasks, partial load, low-clock phases | `rq3_*.csv`, `rq3_split.pdf`, `rq3_partial_load.pdf`, `rq3_low_clock.pdf` |
| `mech.py` | why the memory job slows down next to the CPU job (fixed-time block) | `mech_*.csv`, `mech_memory.pdf`, `mech_cpu_ram_trace.pdf` |
| `controls.py` | governor `performance` vs default; cost of the measurement tools | `controls_*.csv` (no figure) |

CSV results go to `results/rerun_2026-10-31/analysis/`. Figures go to
`thesis/figures/` as PDF, with PNG previews in
`results/rerun_2026-10-31/analysis/preview/`.

## Statistics (the same in every script)

- Main test: Welch t-test, two-sided, 5 vs 5 runs.
- Holm correction over all tests of one family (`rq1`, `rq2`, `rq3`, `mech`,
  `controls`), columns `p_holm` and `holm_ok` in the `*_tests.csv` files.
- Check: exact Mann-Whitney test, not corrected. With 5 vs 5 runs its smallest
  possible p-value is 0.0079.
- Effect size: difference of the means, in units and in % of the reference
  mean, with a 95 % Welch confidence interval.
- Idle reference: mean of the 10 idle runs. Active energy = energy minus idle
  power times the run's duration, for package and DRAM separately (never added).

## Governor check (second session)

The governor check (`bash scripts/bench/run.sh govcheck` on the laptop) runs
three cases of the main session with three power settings. Its archive is
extracted into its own folder:

```
python3 -I scripts/analysis/extract_rerun.py results/rerun_2026-10-31/govcheck thesis_govcheck.tar.gz
```

Its analysis script (`govcheck.py`) is written once the data is there.
