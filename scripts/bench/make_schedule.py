#!/usr/bin/env python3

"""
make_schedule.py - the run order of one session, made from a fixed seed

The session runs in rounds. Each round runs every case of cases.CASES once,
in an order shuffled with random.Random(seed), so slow drift over the night
(room temperature, background programs) spreads evenly over all cases instead
of lining up with one of them. Idle runs come before the first and after the
last round. In sequential RQ2 cases the order of the jobs is shuffled per run
as well, so the same job does not always start on a warm chip.

The schedule is made once on the development machine and committed: the
laptop needs no randomness, and a session that restarts after a crash keeps
the same order.

Usage:
  python -I make_schedule.py --rounds 5 --idle 5 --seed 20261029 --out schedules/main.csv
  python -I make_schedule.py --pilot --seed 20261029 --out schedules/pilot.csv
  python -I make_schedule.py --memcheck --out schedules/memcheck.csv
         [--calibration calibration.env]
Prints the run counts and a time estimate (from the rough times in calibration.env).
"""

import argparse
import csv
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cases  # noqa: E402

COLUMNS = ["order", "run_id", "phase", "round", "position", "case_id", "part_order"]


def make_rows(rounds, idle, seed):
    rng = random.Random(seed)
    rows = []

    def add(run_id, phase, rnd, position, case_id, part_order=""):
        rows.append({"order": len(rows) + 1, "run_id": run_id, "phase": phase, "round": rnd,
                     "position": position, "case_id": case_id, "part_order": part_order})

    for k in range(1, idle + 1):
        add(f"idle_start_{k}", "idle_start", "", k, "idle")
    for r in range(1, rounds + 1):
        ids = [c["case_id"] for c in cases.CASES]
        rng.shuffle(ids)
        for pos, case_id in enumerate(ids, 1):
            case = cases.CASE_BY_ID[case_id]
            order = ""
            if cases.mixed_sequential(case):
                perm = list(range(len(case["parts"])))
                rng.shuffle(perm)
                order = ",".join(map(str, perm))
            add(f"r{r}_{case_id}", "round", r, pos, case_id, order)
    for k in range(1, idle + 1):
        add(f"idle_end_{k}", "idle_end", "", k, "idle")
    return rows


# Pilot: the probes (phase 'probe'), then each main job REPEATS times at its
# calibrated size (phase 'repeat', for repeatability), then one full rehearsal
# round of every case with one idle run before and after.
REPEAT_CASES = ["cpu_t8", "mem_8g", "mem_1g", "io_seq"]


def make_pilot_rows(seed, repeats=3):
    head = [{"run_id": c["case_id"], "phase": "probe", "round": "", "case_id": c["case_id"], "part_order": ""}
            for c in cases.PILOT_CASES]
    head += [{"run_id": f"rep{k}_{cid}", "phase": "repeat", "round": "", "case_id": cid, "part_order": ""}
             for k in range(1, repeats + 1) for cid in REPEAT_CASES]
    for pos, r in enumerate(head, 1):
        r["position"] = pos
    rows = head + make_rows(1, 1, seed)
    for i, r in enumerate(rows, 1):
        r["order"] = i
    return [{k: r[k] for k in COLUMNS} for r in rows]


# Memory check: idle probe, every candidate vm method at every buffer size, then
# (with the chosen method and sizes) each memory job 3 times and the RQ3 memory
# splits once, to see that every size and every split moves the same data.
MEMCHECK_REPEATS = ["mem_1g", "mem_2g", "mem_4g", "mem_8g"]
MEMCHECK_SPLITS = ["rq3_ram_par_n2", "rq3_ram_par_n4", "rq3_ram_par_n8", "rq3_ram_seq_n8"]


def make_memcheck_rows(repeats=3):
    rows = [{"run_id": "probe_idle", "phase": "probe", "case_id": "probe_idle"}]
    rows += [{"run_id": c["case_id"], "phase": "probe", "case_id": c["case_id"]} for c in cases.MEMCHECK_CASES]
    rows += [{"run_id": f"rep{k}_{cid}", "phase": "repeat", "case_id": cid}
             for k in range(1, repeats + 1) for cid in MEMCHECK_REPEATS]
    rows += [{"run_id": f"rep1_{cid}", "phase": "repeat", "case_id": cid} for cid in MEMCHECK_SPLITS]
    for i, r in enumerate(rows, 1):
        r.update(order=i, position=i, round="", part_order="")
    return [{k: r[k] for k in COLUMNS} for r in rows]


def write_schedule(path, rows, rounds, idle, seed, pilot=False, memcheck=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        made = ("--memcheck" if memcheck else f"--pilot --seed {seed}" if pilot
                else f"--rounds {rounds} --idle {idle} --seed {seed}")
        f.write(f"# made by make_schedule.py {made}; {len(cases.CASES)} cases, {len(rows)} runs\n")
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def estimate(rows, cal, rounds):
    """-> (total seconds, {block: (runs, seconds)}) with the rough times of calibration.env."""
    per_block = {}
    total = 0.0
    for row in rows:
        case = cases.CASE_BY_ID[row["case_id"]]
        s = case["expected"](cal) + float(cal["SETTLE_EST_S"]) + float(cal["NXF_START_S"])
        n, t = per_block.get(case["block"], (0, 0.0))
        per_block[case["block"]] = (n + 1, t + s)
        total += s
    total += max(0, rounds - 1) * float(cal["BREAK_S"])
    return total, per_block


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--idle", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--calibration", default=os.path.join(HERE, "calibration.env"))
    ap.add_argument("--pilot", action="store_true", help="pilot schedule: probes, repeats, one rehearsal round")
    ap.add_argument("--memcheck", action="store_true", help="memory check: vm method probes, memory repeats and splits")
    a = ap.parse_args(argv)
    if a.memcheck:
        rows = make_memcheck_rows()
        a.rounds, a.idle = 0, 0
    elif a.pilot:
        rows = make_pilot_rows(a.seed)
        a.rounds, a.idle = 1, 1
    else:
        rows = make_rows(a.rounds, a.idle, a.seed)
    write_schedule(a.out, rows, a.rounds, a.idle, a.seed, pilot=a.pilot, memcheck=a.memcheck)
    cal = cases.load_calibration(a.calibration)
    total, per_block = estimate(rows, cal, a.rounds)
    kind = (" (memory check: method probes, memory repeats and splits)" if a.memcheck else
            " (pilot: probes, repeats, one rehearsal round)" if a.pilot else
            f" ({a.rounds} rounds x {len(cases.CASES)} cases + {2 * a.idle} idle)")
    print(f"{a.out}: {len(rows)} runs{kind}" + ("" if a.memcheck else f", seed {a.seed}"))
    for block, (n, t) in sorted(per_block.items(), key=lambda kv: -kv[1][1]):
        print(f"  {block:<5} {n:>4} runs  {t / 3600:5.1f} h")
    print(f"  breaks between rounds {max(0, a.rounds - 1) * float(cal['BREAK_S']) / 3600:.1f} h")
    print(f"  estimated session time {total / 3600:.1f} h (rough: run times and waits from {os.path.basename(a.calibration)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
