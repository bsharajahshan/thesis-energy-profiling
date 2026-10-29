# Test fixtures

Real tool output from the test laptop (i7-8550U, Ubuntu 24.04, kernel 7.0.0-38), taken unchanged from `thesis-rq3_run_2026-10-15.zip`, folder `01_diagnostics/full_run_60s_5reps/raw/` (diagnostic run of 2026-10-15). Checked: no host or user names inside. Saved as `.txt` because `.gitignore` ignores `*.log`.

| Fixture | Source in the zip | Tool and command |
|---|---|---|
| `stressng-0.17.06_cpu8_60s.txt` | `cpu_ram_1/cpu8.log` | stress-ng 0.17.06, `--cpu 8 --timeout 60s --metrics-brief` |
| `stressng-0.17.06_vm8g_60s.txt` | `cpu_ram_1/vm.log` | stress-ng 0.17.06, `--vm 1 --vm-bytes 8G --timeout 60s --metrics-brief` |
| `stressng-0.17.06_cpu8_load50_60s.txt` | `cpu8_half_1/cpu8h.log` | stress-ng 0.17.06, `--cpu 8 --cpu-load 50 --timeout 60s --metrics-brief` |
| `perf-7.0_stat_plain.csv` | `cpu_ram_1/perf.csv` | perf 7.0.14, `perf stat -a -x, -e instructions,cycles,context-switches,cpu-migrations,uncore_imc/data_reads/,uncore_imc/data_writes/` |
| `fio-3.42_write.json` | `ram_io_1/io.json` | fio 3.42, `--rw=write --bs=4k --ioengine=libaio --iodepth=16 --size=1G --runtime=60 --time_based --output-format=json` |

`perf_interval_ASSUMED.csv` is NOT real output. It is written by hand in the format the perf documentation gives for `perf stat -a -x, -I 1000 --summary` (interval lines start with the time, summary lines with `summary`). The pilot records real interval output on the laptop; it replaces this file before the main session.
