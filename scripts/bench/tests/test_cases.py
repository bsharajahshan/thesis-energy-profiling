#!/usr/bin/env python3

"""
test_cases.py - tests for scripts/bench/cases.py and make_schedule.py

Run (Windows or Linux):  python -I scripts/bench/tests/test_cases.py
"""

import collections
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, BENCH)
import benchlib  # noqa: E402
import cases  # noqa: E402
import make_schedule  # noqa: E402

CAL_PATH = os.path.join(BENCH, "calibration.env")
MAIN_SCHEDULE = os.path.join(BENCH, "schedules", "main.csv")
MAIN_ARGS = {"rounds": 5, "idle": 5, "seed": 20261029}     # documented in schedules/main.csv
LAPTOP = "0,4;1,5;2,6;3,7"        # assumed sibling pairs of the i7-8550U; the session reads the real ones


def cal():
    return cases.load_calibration(CAL_PATH)


def spec_for(case_id, part_order="", topology=LAPTOP):
    row = {"run_id": f"t_{case_id}", "case_id": case_id, "round": 1, "order": 1, "part_order": part_order}
    return cases.build_spec(row, cal(), cases.pin_sets(topology))


class TestCaseList(unittest.TestCase):
    def test_counts(self):
        blocks = collections.Counter(c["block"] for c in cases.CASES)
        self.assertEqual(blocks, {"rq1": 10, "rq2": 8, "rq3": 20, "mech": 6, "gov": 4, "cost": 4})
        ids = [c["case_id"] for c in cases.CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_case_gives_a_valid_spec(self):
        d = tempfile.mkdtemp()
        try:
            for c in cases.CASES + [cases.IDLE_CASE]:
                path = os.path.join(d, "spec.json")
                with open(path, "w", encoding="utf-8") as f:
                    order = ",".join(str(i) for i in range(len(c["parts"]))) if cases.mixed_sequential(c) else ""
                    json.dump(spec_for(c["case_id"], order), f)
                spec = benchlib.load_spec(path)
                plan, _, _ = benchlib.build_plan(spec, {"STRESS_NG": "s", "FIO": "f", "TASKSET": "t"}, "/o")
                self.assertGreater(plan["watchdog_s"], 0)
        finally:
            shutil.rmtree(d)

    def test_rq1_same_cpu_work_for_all_thread_counts(self):
        ops = {spec_for(f"cpu_t{t}")["parts"][0]["ops"] for t in (1, 2, 4, 8)}
        self.assertEqual(ops, {int(cal()["CPU_OPS"])})
        self.assertEqual(spec_for("mem_4g")["parts"][0], {"kind": "vm", "vm_bytes": "4G", "ops": int(cal()["VM_OPS_4G"]),
                                                          "name": "ram"})

    def test_rq3_cpu_split_keeps_total_work_and_threads(self):
        total = int(cal()["CPU_OPS"])
        for prefix in ("rq3_cpu_par_n", "rq3_cpu_seq_n", "rq3_cpu_load50_n", "rq3_cpu_load25_n"):
            for n in (2, 4, 8):
                parts = spec_for(f"{prefix}{n}")["parts"]
                self.assertEqual(len(parts), n)
                self.assertEqual(sum(p["ops"] for p in parts), total, f"{prefix}{n}")
                self.assertEqual(sum(p["threads"] for p in parts), 8)
                self.assertEqual(len({p["name"] for p in parts}), n)
        self.assertEqual(spec_for("rq3_cpu_load25_n4")["parts"][0]["load"], 25)
        self.assertEqual(spec_for("rq3_cpu_seq_n4")["mode"], "sequential")

    def test_rq3_ram_split(self):
        c = cal()
        for mode in ("par", "seq"):
            for n in (2, 4, 8):
                parts = spec_for(f"rq3_ram_{mode}_n{n}")["parts"]
                gb = 8 // n
                self.assertEqual({p["vm_bytes"] for p in parts}, {f"{gb}G"})
                self.assertEqual({p["ops"] for p in parts}, {cases.whole(benchlib.to_int(c[f"VM_OPS_{gb}G"]) / n)})

    def test_rq2_jobs_are_the_rq1_jobs(self):
        s = spec_for("rq2_cpu_ram_io_seq", "2,0,1")
        self.assertEqual([p["name"] for p in s["parts"]], ["io", "cpu", "ram"])
        by_name = {p["name"]: p for p in s["parts"]}
        self.assertEqual({k: v for k, v in by_name["cpu"].items() if k != "name"},
                         {k: v for k, v in spec_for("cpu_t8")["parts"][0].items() if k != "name"})
        self.assertEqual(by_name["io"]["pattern"], "write")
        self.assertEqual(by_name["io"]["bytes"], int(cal()["IO_BYTES"]))
        par = spec_for("rq2_cpu_ram_par")
        self.assertEqual(par["mode"], "parallel")
        self.assertGreater(par["expected_s"], 60)          # longer than one job alone
        with self.assertRaises(ValueError):
            spec_for("rq2_cpu_ram_io_seq", "0,0,1")

    def test_governor_and_logging(self):
        main = spec_for("cpu_t4")
        self.assertEqual((main["governor"], main["epp"], main["logging"]), ("powersave", "balance_performance", True))
        gov = spec_for("gov_cpu_t4")
        self.assertEqual((gov["governor"], gov["epp"]), ("performance", None))
        self.assertEqual(gov["parts"][0]["ops"], main["parts"][0]["ops"])
        for cid in ("cost_cpu_t8_nolog", "cost_mem_8g_nolog", "cost_cpu_ram_par_nolog", "cost_idle_nolog"):
            self.assertFalse(spec_for(cid)["logging"], cid)

    def test_mechanism_fixed_time_and_pins(self):
        for c in cases.CASES:
            if c["block"] == "mech":
                for p in spec_for(c["case_id"])["parts"]:
                    self.assertEqual(p["fixed_s"], 60.0)
                    self.assertNotIn("ops", p)
        own = {p["name"]: p for p in spec_for("mech_pin_own_core")["parts"]}
        self.assertEqual((own["cpu"]["pin"], own["cpu"]["threads"], own["ram"]["pin"]), ("0,1,2,4,5,6", 6, "3"))
        shared = {p["name"]: p for p in spec_for("mech_pin_shared_core")["parts"]}
        self.assertEqual((shared["cpu"]["pin"], shared["cpu"]["threads"], shared["ram"]["pin"]), ("0,1,2,4,5,6,7", 7, "3"))
        self.assertNotIn("pin", spec_for("mech_cpu7_vm")["parts"][0])

    def test_other_topologies(self):
        p = cases.pin_sets("0-1;2-3;4-5;6-7")          # siblings numbered next to each other
        self.assertEqual(p, {"@mem_cpu": "6", "@other_cores": "0,1,2,3,4,5", "@all_but_mem_cpu": "0,1,2,3,4,5,7"})
        no_smt = cases.pin_sets("0;1;2;3")
        self.assertNotIn("@all_but_mem_cpu", no_smt)
        with self.assertRaises(ValueError):
            spec_for("mech_pin_shared_core", topology="0;1;2;3")
        with self.assertRaises(ValueError):           # 6 CPU threads but only 3 CPUs free
            spec_for("mech_pin_own_core", topology="0;1;2;3")

    def test_calibration_checks(self):
        d = tempfile.mkdtemp()
        try:
            with open(CAL_PATH, encoding="utf-8") as f:
                good = f.read()
            path = os.path.join(d, "cal.env")
            for bad, msg in ((good.replace("CPU_OPS=360000", "CPU_OPS=360001"), "multiple of 8"),
                             (good.replace("IO_BYTES=7516192768", "IO_BYTES=7516192769"), "multiple of 4096"),
                             (good.replace("VM_OPS_2G=", "#VM_OPS_2G="), "VM_OPS_2G"),
                             (good.replace("EPP_MAIN=balance_performance", "EPP_MAIN="), "EPP_MAIN")):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(bad)
                with self.assertRaisesRegex(ValueError, msg):
                    cases.load_calibration(path)
        finally:
            shutil.rmtree(d)


class TestSchedule(unittest.TestCase):
    def rows(self, **kw):
        args = dict(MAIN_ARGS)
        args.update(kw)
        return make_schedule.make_rows(args["rounds"], args["idle"], args["seed"])

    def test_shape(self):
        rows = self.rows()
        self.assertEqual(len(rows), 5 * len(cases.CASES) + 10)
        self.assertEqual([r["phase"] for r in rows[:5]], ["idle_start"] * 5)
        self.assertEqual([r["phase"] for r in rows[-5:]], ["idle_end"] * 5)
        self.assertEqual([r["order"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(len({r["run_id"] for r in rows}), len(rows))
        all_ids = sorted(c["case_id"] for c in cases.CASES)
        for rnd in range(1, 6):
            in_round = [r for r in rows if r["round"] == rnd]
            self.assertEqual(sorted(r["case_id"] for r in in_round), all_ids, f"round {rnd}")
            self.assertEqual([r["position"] for r in in_round], list(range(1, len(all_ids) + 1)))

    def test_rounds_are_shuffled_differently(self):
        rows = self.rows()
        orders = [tuple(r["case_id"] for r in rows if r["round"] == rnd) for rnd in range(1, 6)]
        self.assertEqual(len(set(orders)), 5)

    def test_seed_reproduces(self):
        self.assertEqual(self.rows(), self.rows())
        self.assertNotEqual(self.rows(), self.rows(seed=1))

    def test_part_orders(self):
        rows = self.rows()
        for r in rows:
            case = cases.CASE_BY_ID[r["case_id"]]
            if cases.mixed_sequential(case):
                self.assertEqual(sorted(r["part_order"].split(",")), [str(i) for i in range(len(case["parts"]))])
            else:
                self.assertEqual(r["part_order"], "")
        seq3 = {r["part_order"] for r in rows if r["case_id"] == "rq2_cpu_ram_io_seq"}
        self.assertGreater(len(seq3), 1)

    def test_committed_schedule_is_what_the_script_makes(self):
        buf = io.StringIO()
        rows = self.rows()
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "main.csv")
            make_schedule.write_schedule(path, rows, **MAIN_ARGS)
            with open(path, encoding="utf-8") as f:
                buf.write(f.read())
        finally:
            shutil.rmtree(tmp)
        with open(MAIN_SCHEDULE, encoding="utf-8") as f:
            self.assertEqual(f.read(), buf.getvalue())

    def test_specs_command(self):
        d = tempfile.mkdtemp()
        try:
            env = os.path.join(d, "session.env")
            with open(env, "w", encoding="utf-8") as f:
                f.write(f"CPU_SIBLINGS='{LAPTOP}'\n")
            out = os.path.join(d, "specs")
            self.assertEqual(cases.main(["cases.py", "specs", MAIN_SCHEDULE, CAL_PATH, env, out]), 0)
            files = os.listdir(out)
            self.assertEqual(len(files), len(cases.read_schedule(MAIN_SCHEDULE)))
            s = benchlib.load_spec(os.path.join(out, "r3_rq2_cpu_ram_io_seq.json"))
            self.assertEqual((s["round"], s["block"]), (3, "rq2"))
            self.assertEqual(sorted(p["name"] for p in s["parts"]), ["cpu", "io", "ram"])
            with open(env, "w", encoding="utf-8") as f:
                f.write("NCPU='8'\n")                   # no CPU_SIBLINGS -> clear error, exit 1
            self.assertEqual(cases.main(["cases.py", "specs", MAIN_SCHEDULE, CAL_PATH, env, out]), 1)
        finally:
            shutil.rmtree(d)

    def test_time_estimate_plausible(self):
        total, per_block = make_schedule.estimate(self.rows(), cal(), 5)
        self.assertTrue(9 * 3600 < total < 16 * 3600, total / 3600)
        self.assertEqual(sum(n for n, _ in per_block.values()), 5 * len(cases.CASES) + 10)

    def test_pilot_schedule(self):
        rows = make_schedule.make_pilot_rows(MAIN_ARGS["seed"])
        phases = [r["phase"] for r in rows]
        n_probe = len(cases.PILOT_CASES)
        self.assertEqual(phases, ["probe"] * n_probe + ["repeat"] * 12 + ["idle_start"] + ["round"] * len(cases.CASES)
                         + ["idle_end"])
        self.assertEqual((rows[0]["run_id"], rows[n_probe - 1]["run_id"]), ("probe_idle", "probe_cooldown"))
        self.assertEqual([r["order"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(len({r["run_id"] for r in rows}), len(rows))
        self.assertEqual(sorted(r["case_id"] for r in rows if r["phase"] == "round"),
                         sorted(c["case_id"] for c in cases.CASES))
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "pilot.csv")
            make_schedule.write_schedule(path, rows, 1, 1, MAIN_ARGS["seed"], pilot=True)
            with open(path, encoding="utf-8") as a, open(os.path.join(BENCH, "schedules", "pilot.csv"), encoding="utf-8") as b:
                self.assertEqual(a.read(), b.read(), "committed pilot.csv differs from what make_schedule.py makes")
        finally:
            shutil.rmtree(tmp)

    def test_probe_specs(self):
        c = cal()
        self.assertEqual(spec_for("probe_cpu_t8")["parts"][0]["fixed_s"], float(c["PROBE_S"]))
        self.assertEqual(spec_for("probe_cpu_load25")["parts"][0]["load"], 25)
        self.assertEqual(spec_for("probe_mem_2g")["parts"][0]["vm_bytes"], "2G")
        self.assertEqual(spec_for("probe_io_rand")["parts"][0]["bytes"], int(c["PROBE_IO_BYTES"]))
        self.assertEqual(spec_for("probe_ops_semantics")["parts"][0]["ops"], int(c["PROBE_SEMANTICS_OPS"]))
        cool = spec_for("probe_cooldown")
        self.assertEqual(cool["mode"], "sequential")
        self.assertEqual([(p["kind"], p.get("fixed_s"), p.get("seconds")) for p in cool["parts"]],
                         [("cpu", float(c["MECH_FIXED_S"]), None), ("idle", None, float(c["PROBE_COOLDOWN_S"]))])
        d = tempfile.mkdtemp()
        try:
            for case in cases.PILOT_CASES:
                path = os.path.join(d, "spec.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(spec_for(case["case_id"]), f)
                benchlib.load_spec(path)
        finally:
            shutil.rmtree(d)

    def test_schedule_file_is_readable_with_csv(self):
        with open(MAIN_SCHEDULE, encoding="utf-8") as f:
            first = f.readline()
            self.assertTrue(first.startswith("# made by make_schedule.py"))
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["run_id"], "idle_start_1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
