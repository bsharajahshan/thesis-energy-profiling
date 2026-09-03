# A Reference Workflow Benchmark for System-Level Energy Profiling

Bachelor's thesis at Humboldt-Universität zu Berlin. This project uses a controlled benchmark to answer three research questions about energy behavior in CPU, memory, and I/O subsystems.

See **CLAUDE.md** for full project documentation, research questions, and scientific constraints. (Note: `CLAUDE.md`, `KANBAN.md`, and `DEVLOG.md` are local working notes and are not tracked in git — they will not be present after a fresh clone.)

---

## Quick Start

### Prerequisites (Linux Machine Only)

You need these tools installed on the **Linux machine** where experiments run:

```bash
# Nextflow (workflow orchestration)
curl -s https://get.nextflow.io | bash
chmod +x nextflow

# stress-ng (synthetic workloads)
sudo apt-get install stress-ng

# fio (I/O benchmarking)
sudo apt-get install fio

# Python dependencies (for analysis)
pip install pandas matplotlib scipy
```

### Verify RAPL Access

RAPL measurements only work on Linux machines with Intel processors:

```bash
cat /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj
```

Should output a number (energy in microjoules). If permission denied, you may need:

```bash
sudo chmod 644 /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj
```

This resets on reboot — re-run it after restarting the machine.

---

## Validating RAPL Readings

Because RAPL is the sole energy source for this benchmark, its readings are cross-checked against `turbostat` — an independent tool that reads the same underlying hardware counters through a separate code path. See `scripts/validate_rapl.sh`.

### Additional prerequisites for validation

```bash
# Install turbostat
sudo apt-get install linux-tools-common linux-tools-$(uname -r)
# if that specific kernel package isn't found:
sudo apt-get install linux-tools-generic
which turbostat

# Enable MSR access (turbostat reads raw MSRs via /dev/cpu/*/msr)
sudo modprobe msr
ls /dev/cpu/0/msr    # should now exist

# Confirm turbostat reports RAPL power on this CPU
sudo turbostat --show PkgWatt,RAMWatt -- sleep 5
```

turbostat requires root, same as the RAPL sysfs counter. Run the validation script as root (or with passwordless sudo configured) so every repetition completes without an interactive password prompt.

### Running validation

```bash
sudo scripts/validate_rapl.sh "stress-ng --cpu 4 --timeout 20s" cpu_check 5
```

This runs the given command 5 times, measuring each run's energy two independent ways (direct RAPL sysfs read, and turbostat wrapping the identical command) and reports the per-rep and mean discrepancy. Results are appended to `results/raw/validation/rapl_validation.csv`. A mean discrepancy above 10% is flagged — investigate before trusting RAPL data from the main pipeline.

Run this once after setting up a new machine, and again if you change kernel, CPU governor, or power settings.

---

## Running Experiments

All commands run on the **Linux machine**. Development happens on Windows (edit, commit, push); Linux pulls and executes.

**Implementation status:**
- **RQ1 (`baseline.nf`)** — implemented, smoke-tested end-to-end.
- **RQ2 (`mixed_load.nf`)** — implemented, **not yet run on Linux**. Smoke-test it (see below) before trusting its output.
- **RQ3 (`granularity.nf`)** — implemented, **not yet run at all**. Smoke-test it (see below) before a full run.

**Before running anything above, note:** `nextflow.config` was recently changed to cap the local executor's resource pool at exactly one task's worth (`executor.$local.cpus = 4`, matching `process.cpus`). Without this, Nextflow could legally run two task instances concurrently, and since RAPL is one shared system-wide counter, overlapping measurement windows would silently corrupt both readings. This change hasn't been exercised on real hardware yet either — the first smoke test you run (any of RQ1/RQ2/RQ3 below) doubles as a check that Nextflow accepts it and doesn't stall.

### RQ1: Baseline Subsystem Profiling

Run CPU baseline (1, 2, 4, 8 threads) with 5 repetitions each:

```bash
nextflow run workflows/main.nf \
    --mode baseline \
    --subsystem cpu \
    --duration 60 \
    --repetitions 5
```

Similarly for memory and I/O:

```bash
# Memory baseline (1, 2, 4, 8 GB)
nextflow run workflows/main.nf \
    --mode baseline \
    --subsystem memory \
    --duration 60 \
    --repetitions 5

# I/O baseline (sequential, random patterns)
nextflow run workflows/main.nf \
    --mode baseline \
    --subsystem io \
    --duration 60 \
    --repetitions 5
```

Output lands in `results/raw/rq1_baseline/cpu/`, `rq1_baseline/memory/`, `rq1_baseline/io/` (one CSV per intensity × repetition).

### RQ2: Mixed-Load Behaviour

Compares sequential vs parallel execution of complementary subsystem combinations — **all cross-subsystem pairs plus the three-way combination**: `cpu_ram`, `cpu_io`, `ram_io`, `cpu_ram_io`. There is no same-resource "competing" condition and no intensity ratio sweep — every combination runs at a fixed MAX intensity level, matched exactly to the RQ1 baseline (CPU=8 threads, RAM=8GB, I/O=sequential pattern), so results are directly comparable to RQ1 data points.

```bash
nextflow run workflows/main.nf \
    --mode mixed \
    --duration 60 \
    --repetitions 5
```

This runs all 4 combinations × 2 modes (sequential, parallel) × repetitions = 40 runs by default. Output lands in `results/raw/rq2_mixed/sequential/` and `rq2_mixed/parallel/` (one CSV per combination × repetition).

### RQ3: Task Granularity

Compares one large task against N smaller concurrent sub-tasks doing the same total work — CPU and RAM only, no I/O. N = 1, 2, 4, 8 sub-tasks; splitting divides the *resource* per sub-task (cores for CPU, GB for RAM), not the duration — each sub-task still runs for the full `--duration`. Two modes run automatically for every N: **parallel** (the main experiment — sub-tasks run concurrently, one RAPL window) and **sequential** (a secondary reference — sub-tasks run back-to-back, own RAPL windows, summed — used to isolate startup overhead from real concurrency effects). RAM sub-tasks are deliberately not bandwidth-capped; any contention between them is a reportable finding, not a bug.

```bash
nextflow run workflows/main.nf \
    --mode granularity \
    --duration 60 \
    --repetitions 5
```

This runs 2 subsystems × 4 N-values × 2 modes × repetitions = 80 runs by default. Output lands in `results/raw/rq3_granularity/cpu/` and `rq3_granularity/ram/` (one CSV per N × mode × repetition).

**Before trusting RQ3 results**, run the work-equivalence pre-flight check — see "Work-Equivalence Check" below.

---

## Testing Before Full Experiments

Test with shorter duration and fewer repetitions first.

### RQ1 smoke test

```bash
# Quick smoke test (1 thread, 10 seconds, 1 rep)
nextflow run workflows/main.nf \
    --mode baseline \
    --subsystem cpu \
    --duration 10 \
    --repetitions 1
```

Check results:

```bash
cat results/raw/rq1_baseline/cpu/cpu_baseline_*.csv
```

Expected output (one CSV row per run):

```
rq1_cpu_baseline,cpu_baseline,sequential,cpu,1,1,10.123,45678900,45.679,1,2026-06-28T14:30:45Z
```

Columns: `experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,run_id,timestamp`

### RQ2 smoke test

RQ2 has not been run on Linux yet — always smoke-test before a full run:

```bash
# Quick smoke test (all 4 combinations x 2 modes, 10 seconds, 1 rep)
nextflow run workflows/main.nf \
    --mode mixed \
    --duration 10 \
    --repetitions 1
```

Check results:

```bash
cat results/raw/rq2_mixed/sequential/*.csv
cat results/raw/rq2_mixed/parallel/*.csv
```

Expected: 4 CSV files in each of `sequential/` and `parallel/`. Columns: `experiment_id,task_name,mode,combination,intensity,task_count,duration_s,energy_uj,energy_j,cpu_util_pct,mem_util_pct,run_id,timestamp`. `cpu_util_pct`/`mem_util_pct` are sampled directly from `/proc` during the run — a non-zero, plausible value there is independent evidence the workload actually loaded the system, separate from the RAPL energy reading itself.

### RQ3 smoke test

RQ3 has never been run at all — always smoke-test before a full run:

```bash
# Quick smoke test (2 subsystems x 4 N-values x 2 modes, 10 seconds, 1 rep)
nextflow run workflows/main.nf \
    --mode granularity \
    --duration 10 \
    --repetitions 1
```

Check results:

```bash
cat results/raw/rq3_granularity/cpu/*.csv
cat results/raw/rq3_granularity/ram/*.csv
```

Expected: 8 CSV files in each of `cpu/` and `ram/` (N=1,2,4,8 × parallel/sequential). Columns: `experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,run_id,timestamp` — the general schema, no `cpu_util_pct`/`mem_util_pct` (unlike RQ2). `intensity` should read `max` on every row; `task_count` should match N. For a given N, the `sequential` row's `duration_s` should be roughly N times the `parallel` row's `duration_s` (parallel runs all sub-tasks at once; sequential runs them back-to-back) — if they're roughly equal instead, something didn't launch the sub-tasks correctly.

### Work-equivalence check

Before trusting any real RQ3 data, confirm the N-way split is actually doing equivalent total work at every N (see `CLAUDE.md`, Key Scientific Constraints — "without this, RQ3 has no scientific validity"):

```bash
bash scripts/verify_work_equivalence.sh 30
```

(the argument is duration in seconds — 30s is a reasonable default for this check, shorter than the real 60s experiment runs). No root/RAPL access needed — it's a standalone check, independent of Nextflow. It prints a table per subsystem × mode; compare `total-bogo-ops` across N=1,2,4,8 within each table:

- **CPU** should be roughly flat across N in both modes.
- **RAM** should be roughly flat in `sequential`, but may decline with N in `parallel` — that decline, if present, is a real memory-bandwidth-contention effect (expected and reportable, not a bug).
- Large unexplained drift on the CPU side, or in RAM's sequential row, means the work-equivalence premise is broken and RQ3 needs revisiting before collecting real data.

This is the first time this script has been run against a real `stress-ng` install — if it prints `WARNING: could not parse bogo-ops...`, the raw output it dumps to stderr will show the actual line format `stress-ng --metrics-brief` produced on this machine, which the `extract_bogo_ops` awk pattern near the top of the script may need adjusting to match.

---

## Analysis

After experiments, clean and analyze results:

```bash
# 1. Clean data (remove outliers, validate schema)
python3 scripts/analysis/clean_data.py results/raw/rq1_baseline/cpu/cpu_baseline_*.csv results/clean_cpu.csv

# 2. Compute statistics
python3 scripts/analysis/stats.py results/clean_cpu.csv

# 3. Generate plots
python3 scripts/analysis/plot.py results/clean_cpu.csv figures/
```

Plots are saved to `figures/rq1_load_curves.pdf`, `figures/rq2_energy_comparison.pdf`, etc.

Note: `clean_data.py`/`stats.py` currently assume the RQ1 schema (numeric `intensity`, `subsystem`/`task_name` grouping). RQ2's schema differs (`combination` instead of `subsystem`, `intensity` fixed at `"max"`) and isn't yet supported by these scripts — treat RQ2 analysis as a manual/ad-hoc step for now.

---

## Resume Failed Runs

If an experiment is interrupted, Nextflow caches completed tasks. Resume:

```bash
nextflow run workflows/main.nf -resume
```

---

## Directory Structure

```
workflows/              Nextflow pipeline files
scripts/                Measurement and analysis scripts
results/raw/            Raw CSV results (gitignored)
  rq1_baseline/
    cpu/
    memory/
    io/
  rq2_mixed/
    sequential/
    parallel/
  rq3_granularity/
    cpu/
    ram/
  validation/            rapl_validation.csv
figures/                 Generated plots (gitignored)
CLAUDE.md                Full project documentation (local only, gitignored)
```

---

## Important Notes

- **RAPL measurements only work on Linux** with Intel processors. Do not attempt on Windows.
- **Development on Windows:** Write code, commit, push.
- **Experiments on Linux:** Pull, run Nextflow, collect results.
- Minimum **5 repetitions** per configuration for statistical validity.
- If coefficient of variation (CV) > 3%, investigate noise sources before continuing.
- RAPL readings should be validated against `turbostat` (see above) before being trusted for analysis.
- **RQ2 is implemented but unverified** — always run the RQ2 smoke test before a full 60s/5-rep run.
- **RQ3 is implemented but has never been run** — always run the RQ3 smoke test, then `verify_work_equivalence.sh`, before a full 60s/5-rep run.
- The `nextflow.config` executor cap (see Implementation status above) is also untested on real hardware — the first smoke test you run exercises it too.

---

## Troubleshooting

**"RAPL not available at /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"**
- Verify you're on Linux with Intel CPU
- Check: `cat /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj`
- May need: `sudo chmod 644 /sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj`

**"stress-ng: command not found"**
- Install: `sudo apt-get install stress-ng`

**"fio: command not found"**
- Install: `sudo apt-get install fio`

**"turbostat: command not found"**
- Install: `sudo apt-get install linux-tools-common linux-tools-$(uname -r)` (or `linux-tools-generic`)

**turbostat prints no PkgWatt/RAMWatt values**
- Confirm `/dev/cpu/0/msr` exists (`sudo modprobe msr`)
- Confirm the CPU is Intel and supports RAPL

**Nextflow workflow validation fails**
- Check Nextflow version: `nextflow --version` (requires v24.x+)
- Verify DSL2 enabled in `nextflow.config`

---

For detailed project info, see **CLAUDE.md**.
