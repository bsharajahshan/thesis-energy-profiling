#!/usr/bin/env python3

"""
test_check.py - tests for scripts/bench/check_session.py and the summary that uses it

Builds session folders by hand (result.csv / parts.csv / verdict.txt as
run_case.sh writes them) and plants problems: a missing run, an overlap with a
failed attempt, a valid run with the wrong governor, incomplete work, a power
cable out. test_session.sh checks the same in a real session.

Run:  python -I scripts/bench/tests/test_check.py
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, BENCH)
import benchlib  # noqa: E402
import check_session  # noqa: E402
import sessionlib  # noqa: E402

SCHED = """# test
order,run_id,phase,round,position,case_id,part_order
1,idle_start_1,idle_start,,1,idle,
2,r1_cpu_t8,round,1,1,cpu_t8,
3,r1_mem_1g,round,1,2,mem_1g,
4,r2_cpu_t8,round,2,1,cpu_t8,
5,idle_end_1,idle_end,,1,idle,
"""
T0 = 1_793_000_000_000_000            # microseconds


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Session:
    def __init__(self):
        self.res = tempfile.mkdtemp()
        self.dir = os.path.join(self.res, "main")
        put(os.path.join(self.dir, "schedule.csv"), SCHED)
        put(os.path.join(self.dir, "session.env"), "MAX_ATTEMPTS=3\n")
        shutil.copyfile(os.path.join(BENCH, "calibration.env"), os.path.join(self.dir, "calibration.env"))

    def attempt(self, run_id, case_id, att, start_s, valid=True, **over):
        """one attempt; its window is 60 s from T0 + start_s seconds"""
        row = {c: None for c in benchlib.RESULT_COLUMNS}
        idle = case_id == "idle"
        row.update(run_id=run_id, case_id=case_id, attempt=int(att[1:]), max_attempts=3, mode="parallel",
                   logging=True, governor_planned="powersave", epp_planned="balance_performance",
                   governor_start="powersave", epp_start="balance_performance", governor_end="powersave",
                   epp_end="balance_performance", t_start_us=T0 + int(start_s * 1e6),
                   t_end_us=T0 + int((start_s + 60) * 1e6), duration_s=60.0,
                   pkg_energy_j=97.2 if idle else 1500.0, dram_energy_j=17.4 if idle else 60.0,
                   pkg_power_w=1.62 if idle else 25.0, dram_power_w=0.29 if idle else 1.0,
                   perf_pkg_energy_j=97.3 if idle else 1501.0, instructions=1e9,
                   cpu_ops_target=None if idle else 360000, cpu_ops_done=None if idle else 360000,
                   temp_start_c=50.0, settle_timeout=False, thr_pkg_count=0, thr_core_count=2,
                   ac_start=1, ac_end=1, other_cpu_per_s=0.01, valid=valid,
                   hard_reasons=None if valid else "disturbed", soft_flags=None)
        row.update(over)
        d = os.path.join(self.dir, "runs", run_id, att)
        os.makedirs(d, exist_ok=True)
        benchlib.write_csv(os.path.join(d, "result.csv"), benchlib.RESULT_COLUMNS, [row])
        part = {c: None for c in benchlib.PARTS_COLUMNS}
        part.update(run_id=run_id, attempt=int(att[1:]), part_index=0, part_name="idle" if idle else "cpu",
                    kind="idle" if idle else "cpu", complete=True)
        benchlib.write_csv(os.path.join(d, "parts.csv"), benchlib.PARTS_COLUMNS, [part])
        put(os.path.join(d, "verdict.txt"), ("VALID\nhard: -\nsoft: -\n") if valid else "RETRY\nhard: disturbed\nsoft: -\n")
        if valid:
            put(os.path.join(self.dir, "runs", run_id, "VALID"), att + "\n")

    def all_good(self):
        self.attempt("idle_start_1", "idle", "a1", 0)
        self.attempt("r1_cpu_t8", "cpu_t8", "a1", 100)
        self.attempt("r1_mem_1g", "mem_1g", "a1", 200, vm_ops_target=80, vm_ops_done=80,
                     cpu_ops_target=None, cpu_ops_done=None)
        self.attempt("r2_cpu_t8", "cpu_t8", "a1", 300)
        self.attempt("idle_end_1", "idle", "a1", 400, pkg_power_w=1.70)

    def check(self):
        return check_session.check(self.dir, "main")

    def close(self):
        shutil.rmtree(self.res)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.s = Session()

    def tearDown(self):
        self.s.close()

    def line(self, text, topic):
        return next(l for l in text.splitlines() if l.split()[1:2] == [topic])

    def test_all_good(self):
        self.s.all_good()
        text, ok = self.s.check()
        self.assertTrue(ok, text)
        self.assertTrue(text.rstrip().endswith("RESULT: PASSED"))
        self.assertIn("5 of 5 planned runs valid (idle start 1/1, round 3/3, idle end 1/1)", text)
        self.assertIn("no two measurement windows overlap (5 windows incl. failed attempts; smallest gap 40.0 s)", text)
        self.assertIn("package at idle: start 1.62 W, end 1.70 W (+4.9 %)", text)
        self.assertIn("5 valid run(s) with thermal throttling events (10 events in total)", text)
        with open(os.path.join(self.s.dir, "results_all.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["run_id"] for r in rows], ["idle_start_1", "r1_cpu_t8", "r1_mem_1g", "r2_cpu_t8", "idle_end_1"])
        self.assertEqual(list(rows[0])[:6], ["order", "phase", "round", "position", "part_order", "run_id"])
        self.assertEqual(rows[1]["phase"], "round")
        with open(os.path.join(self.s.dir, "parts_all.csv"), encoding="utf-8") as f:
            parts = list(csv.DictReader(f))
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[1]["case_id"], "cpu_t8")

    def test_missing_run(self):
        self.s.all_good()
        shutil.rmtree(os.path.join(self.s.dir, "runs", "r2_cpu_t8"))
        self.s.attempt("r2_cpu_t8", "cpu_t8", "a1", 300, valid=False)
        text, ok = self.s.check()
        self.assertFalse(ok)
        self.assertIn("FAIL  complete", text)
        self.assertIn("1 of 5 planned runs have no valid attempt: r2_cpu_t8", text)
        with open(os.path.join(self.s.dir, "attempts_all.csv"), encoding="utf-8") as f:
            att = [r for r in csv.DictReader(f) if r["run_id"] == "r2_cpu_t8"]
        self.assertEqual((att[0]["verdict"], att[0]["hard_reasons"], att[0]["is_the_valid_attempt"]),
                         ("RETRY", "disturbed", "0"))       # yes/no as 1/0, like all tables

    def test_overlap_with_a_failed_attempt(self):
        self.s.all_good()
        self.s.attempt("r1_mem_1g", "mem_1g", "a2", 150, valid=False)     # 150-210 s overlaps 100-160 and 200-260
        text, ok = self.s.check()
        self.assertFalse(ok)
        self.assertIn("FAIL  overlap", text)
        self.assertIn("r1_cpu_t8/a1 and r1_mem_1g/a2 by 10.0 s", text)
        self.assertIn("r1_mem_1g/a2 and r1_mem_1g/a1 by 10.0 s", text)

    def test_conditions(self):
        self.s.all_good()
        self.s.attempt("r1_cpu_t8", "cpu_t8", "a1", 100, governor_start="performance")
        self.s.attempt("r2_cpu_t8", "cpu_t8", "a1", 300, cpu_ops_done=359999, ac_end=0)
        text, ok = self.s.check()
        self.assertFalse(ok)
        line = self.line(text, "conditions")
        self.assertTrue(line.startswith("FAIL"))
        self.assertIn("r1_cpu_t8: governor performance instead of powersave", line)
        self.assertIn("r2_cpu_t8: ac_end=0, cpu_ops_done 359999 < cpu_ops_target 360000", line)

    def test_repair_pass_and_order(self):
        self.s.all_good()
        shutil.rmtree(os.path.join(self.s.dir, "runs", "r1_mem_1g"))
        for k in (1, 2, 3):
            self.s.attempt("r1_mem_1g", "mem_1g", f"a{k}", 200 + 0.1 * k, valid=False, t_end_us=T0 + int((200.1 * k) * 1e3))
        self.s.attempt("r1_mem_1g", "mem_1g", "a4", 500, vm_ops_target=80, vm_ops_done=80,
                       cpu_ops_target=None, cpu_ops_done=None)
        # like the pilot's killed call: valid on attempt 2, but in the repair pass
        put(os.path.join(self.s.dir, "progress.txt"),
            "2026-11-04T20:00:00Z r1_mem_1g MISSING round=1 pass=main attempts=3 nextflow_exit=0 seconds=300\n"
            "2026-11-04T20:05:00Z r1_cpu_t8 VALID round=1 pass=main attempts=1 nextflow_exit=0 seconds=90\n"
            "2026-11-04T23:00:00Z r1_mem_1g VALID round=1 pass=repair attempts=4 nextflow_exit=0 seconds=80\n"
            "2026-11-04T23:02:00Z r2_cpu_t8 VALID round=2 pass=repair attempts=2 nextflow_exit=0 seconds=80\n")
        text, ok = self.s.check()
        self.assertIn("valid runs started in schedule order except 1 place(s); 2 run(s) became valid only in the repair pass",
                      text)

    def test_summary_first_line(self):
        self.s.all_good()
        self.assertTrue(sessionlib.summary_text(self.s.dir, "main", final=True).startswith(
            "CHECK FAILED: the end check did not run"))
        self.s.check()
        summary = sessionlib.summary_text(self.s.dir, "main", final=True)
        self.assertEqual(summary.splitlines()[0], "DONE")
        self.assertIn("end check (check.txt):", summary)
        self.assertIn("RESULT: PASSED", summary)
        self.s.attempt("r1_mem_1g", "mem_1g", "a2", 150, valid=False)
        os.remove(os.path.join(self.s.dir, "runs", "r2_cpu_t8", "VALID"))
        self.s.check()
        self.assertEqual(sessionlib.summary_text(self.s.dir, "main", final=True).splitlines()[0],
                         "MISSING: 1 of 5 runs: r2_cpu_t8; CHECK FAILED: overlap (see check.txt)")

    def test_command_line(self):
        import contextlib
        import io
        self.s.all_good()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_session.main(["check_session.py", self.s.dir, "main"]), 0)
            self.s.attempt("r1_cpu_t8", "cpu_t8", "a1", 100, pkg_energy_j=0)
            self.assertEqual(check_session.main(["check_session.py", self.s.dir, "main"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
