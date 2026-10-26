# Pilot A: instructions for the test laptop (about 55 minutes)

Why: the new measurement session counts the work done in every run. This pilot checks that all tools work on the
laptop and collects the data that decides the settings (stress-ng methods, size of the jobs). It does not produce
thesis results. Please run it once, under the same conditions as the earlier sessions.

## Before you start (5 minutes)

1. Power cable connected. Power profile `performance` (`powerprofilesctl set performance`). Do not change anything else.
2. Laptop flat on the desk, same place as for the earlier sessions, nothing else running: close the browser, stop the
   AI agents and background programs (opencode, hermes, ollama, remote desktop). Best: run the pilot from a text
   terminal or over SSH, not from a desktop terminal.
3. Check that these programs exist: `stress-ng fio perf turbostat taskset flock nextflow python3`.
   (`flock` is in the package `util-linux`, `perf` in `linux-tools-generic`.) Missing ones are listed in `checks.txt`.
4. Update the repo: `git pull` (use the branch you normally use for the experiments; it must contain the files
   `scripts/pilot_a.sh`, `scripts/run_case.sh` and the folder `scripts/shlib`).

## Run it

```bash
cd ~/work/thesis-energy-profiling        # or wherever the repo is
sudo -v                                  # sudo is needed for perf, turbostat and the RAPL files
bash scripts/pilot_a.sh --smoke          # about 3 minutes: only checks that the script itself works
```

Look at the last lines: they list every check as PASS or FAIL. Then the real pilot:

```bash
sudo -v
bash scripts/pilot_a.sh                  # about 50 minutes; keep the laptop alone while it runs
```

The script prints what it is doing. At the end it writes `pilot_a_<date>.tar.gz` next to the folder.

## Send back

- the file `pilot_a_<date>.tar.gz`
- a short note: anything that went wrong, anything you changed on the laptop, the room (is it cold or warm?).

## If something fails

- A line `FAIL ...` in `checks.txt` is not a problem for you. It tells me what to change. Just send the file.
- If the script stops with an error message, send the message and the folder `pilot_a_<date>`.
- `sudo` asks for the password again after 15 minutes only if you run the script without `sudo -v` first.
  The script starts everything that needs root at the very beginning.
