#!/usr/bin/env python3

"""
test_calibrate.py - tests for scripts/bench/calibrate.py and pilot_report.py

The probe results are made up with round numbers, so every calibrated value
can be checked by hand. test_session.sh runs both scripts in a real
(simulated) pilot.

Run:  python -I scripts/bench/tests/test_calibrate.py
"""

import contextlib
import csv
import io
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, BENCH)
import benchlib  # noqa: E402
import calibrate  # noqa: E402
import cases  # noqa: E402
import pilot_report  # noqa: E402

GIB = 1024 ** 3
MIB = 1024 ** 2
T0 = 1_793_000_000_000_000


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


class Pilot:
    """A pilot session folder with hand-made probe results."""

    def __init__(self):
        self.res = tempfile.mkdtemp()
        self.dir = os.path.join(self.res, "pilot")
        shutil.copyfile(os.path.join(BENCH, "calibration.env"), os.path.join(self.dir + "_cal"))
        os.makedirs(self.dir)
        shutil.move(self.dir + "_cal", os.path.join(self.dir, "calibration.env"))
        put(os.path.join(self.dir, "preflight.txt"),
            "INFO  governor     now: governor powersave, EPP balance_power\nRESULT: READY\n")

    def run(self, run_id, res=None, parts=(), sampler=None):
        d = os.path.join(self.dir, "runs", run_id, "a1")
        os.makedirs(d, exist_ok=True)
        row = {c: None for c in benchlib.RESULT_COLUMNS}
        row.update(run_id=run_id, case_id=run_id, valid=True, duration_s=30.0)
        row.update(res or {})
        benchlib.write_csv(os.path.join(d, "result.csv"), benchlib.RESULT_COLUMNS, [row])
        rows = []
        for i, p in enumerate(parts):
            r = {c: None for c in benchlib.PARTS_COLUMNS}
            r.update(run_id=run_id, attempt=1, part_index=i)
            r.update(p)
            rows.append(r)
        benchlib.write_csv(os.path.join(d, "parts.csv"), benchlib.PARTS_COLUMNS, rows)
        if sampler:
            put(os.path.join(d, "sampler.txt"), sampler)
        put(os.path.join(self.dir, "runs", run_id, "VALID"), "a1\n")

    def all_probes(self, skip=()):
        g_idle = 0.03                                        # GiB/s DRAM traffic at idle
        probes = {
            "probe_idle": dict(res=dict(duration_s=60.0, dram_read_gib=1.2, dram_write_gib=0.6, other_cpu_per_s=0.02,
                                        temp_start_c=45.0, pkg_power_w=1.6, dram_power_w=0.29),
                               parts=[dict(kind="idle", done=60.0, duration_s=60.0)]),
            "probe_ops_semantics": dict(parts=[dict(kind="cpu", threads=4, done=80000, flag_value=20000, duration_s=14.0)]),
        }
        for t, rate in ((1, 1500), (2, 2800), (4, 4900), (8, 6000)):
            probes[f"probe_cpu_t{t}"] = dict(parts=[dict(kind="cpu", threads=t, done=rate * 30, duration_s=30.0)])
        for load, rate in ((50, 4000), (25, 2000)):
            probes[f"probe_cpu_load{load}"] = dict(parts=[dict(kind="cpu", threads=8, done=rate * 30, duration_s=30.0)])
        for gb, rate, above in ((1, 320000, 4.0), (2, 160000, 4.5), (4, 80000, 4.8), (8, 40000, 5.0)):
            gib = (above + g_idle) * 30
            probes[f"probe_mem_{gb}g"] = dict(res=dict(dram_read_gib=gib * 0.6, dram_write_gib=gib * 0.4),
                                              parts=[dict(kind="vm", done=rate * 30, duration_s=30.0)])
        probes["probe_io_seq"] = dict(parts=[dict(kind="io", done=4 * GIB, duration_s=35.0)])
        probes["probe_io_rand"] = dict(parts=[dict(kind="io", done=4 * GIB, duration_s=70.0)])
        heavy_end = T0 + 60_000_000
        samples = "".join(f"{(T0 + s * 1_000_000) / 1e6:.6f} 1 1 "
                          f"{int((97 if s <= 60 else 50 + 47 * math.exp(-(s - 60) / 40)) * 1000)}\n"
                          for s in range(0, 361))
        probes["probe_cooldown"] = dict(parts=[dict(kind="cpu", t_end_us=heavy_end, duration_s=60.0),
                                               dict(kind="idle", duration_s=300.0)], sampler=samples)
        for rid, p in probes.items():
            if rid not in skip:
                self.run(rid, p.get("res"), p.get("parts", ()), p.get("sampler"))

    def cal(self):
        return benchlib.read_env(os.path.join(self.dir, "calibration.env"))

    def close(self):
        shutil.rmtree(self.res)


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self.p = Pilot()

    def tearDown(self):
        self.p.close()

    def test_all_values(self):
        self.p.all_probes()
        rc = quiet(calibrate.main, ["calibrate.py", self.p.dir])
        self.assertEqual(rc, 0)
        c = self.p.cal()
        self.assertEqual(c["OPS_SEMANTICS"], "per_instance")             # 80000 done for 20000 asked, 4 workers
        self.assertEqual(c["CPU_OPS"], "360000")                         # 6000 ops/s x 60 s
        self.assertEqual((c["CPU_S_T1"], c["CPU_S_T2"], c["CPU_S_T4"], c["CPU_S_T8"]), ("240.0", "128.6", "73.5", "60.0"))
        self.assertEqual((c["CPU_S_T8_LOAD50"], c["CPU_S_T8_LOAD25"]), ("90.0", "180.0"))
        self.assertEqual(c["VM_OPS_8G"], "2400000")                      # 300 GiB / (5 GiB/s / 40000 ops/s)
        self.assertEqual(c["VM_OPS_1G"], "24000000")                     # 300 GiB / (4 GiB/s / 320000 ops/s)
        self.assertEqual(c["VM_OPS_2G"], str(round(300 / (4.5 / 160000) / 8) * 8))
        self.assertEqual(c["VM_S"], "75.0")                              # 1 GB job: 24e6 / 320000
        io_bytes = int((4 * GIB / 35.0) * 60 // MIB) * MIB
        self.assertEqual(c["IO_BYTES"], str(io_bytes))
        self.assertEqual(c["IO_S_SEQ"], f"{io_bytes / (4 * GIB / 35.0):.1f}")
        self.assertEqual(c["IO_S_RAND"], f"{io_bytes / (4 * GIB / 70.0):.1f}")
        self.assertEqual(c["SETTLE_TEMP_C"], "61")                       # 50 + 47 e^-1.5 = 60.5 -> 61
        self.assertEqual(c["OTHER_CPU_LIMIT"], "0.10")                   # 2 x 0.02 is below the floor
        self.assertEqual(c["EPP_MAIN"], "balance_power")
        cases.load_calibration(os.path.join(self.p.dir, "calibration.env"))      # still a valid file
        with open(os.path.join(self.p.dir, "calibration.env"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("# CALIBRATED by the pilot"))
        self.assertIn("# CPU job: stress-ng --cpu-method all", text)               # comments kept
        self.assertTrue(os.path.exists(os.path.join(self.p.dir, "calibration_before_pilot.env")))
        report = benchlib.read_text(os.path.join(self.p.dir, "calibration_report.txt"))
        self.assertIn("8 thread(s): 6000.0 ops/s", report)
        self.assertIn("RESULT: calibration.env written", report)
        self.assertIn("60 s: 60 C", report)

    def test_missing_probes_keep_old_values(self):
        self.p.all_probes(skip=("probe_cpu_load25", "probe_io_rand", "probe_mem_8g", "probe_cooldown"))
        old = self.p.cal()
        quiet(calibrate.main, ["calibrate.py", self.p.dir])
        c = self.p.cal()
        self.assertEqual(c["CPU_OPS"], "360000")
        for key in ("CPU_S_T8_LOAD25", "IO_S_RAND", "VM_OPS_1G", "VM_OPS_8G", "SETTLE_TEMP_C"):
            self.assertEqual(c[key], old[key], key)
        report = benchlib.read_text(os.path.join(self.p.dir, "calibration_report.txt"))
        self.assertIn("VM_OPS_1G kept at", report)
        self.assertIn("probe_mem_8g (the target) not valid", report)

    def test_nothing_measured(self):
        rc = quiet(calibrate.main, ["calibrate.py", self.p.dir])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "calibration_before_pilot.env")))
        self.assertIn("nothing written", benchlib.read_text(os.path.join(self.p.dir, "calibration_report.txt")))

    def test_total_semantics_and_high_background(self):
        self.p.all_probes(skip=("probe_ops_semantics", "probe_idle"))
        self.p.run("probe_ops_semantics", parts=[dict(kind="cpu", threads=4, done=20003, flag_value=20000, duration_s=4.0)])
        self.p.run("probe_idle", res=dict(duration_s=60.0, dram_read_gib=1.2, dram_write_gib=0.6, other_cpu_per_s=0.4,
                                          temp_start_c=45.0), parts=[dict(kind="idle", done=60.0, duration_s=60.0)])
        quiet(calibrate.main, ["calibrate.py", self.p.dir])
        self.assertEqual(self.p.cal()["OPS_SEMANTICS"], "total")
        self.assertEqual(self.p.cal()["OTHER_CPU_LIMIT"], "0.50")       # 2 x 0.4, capped


def write_table(path, columns, rows):
    full = [{c: r.get(c, "") for c in columns} for r in rows]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(full)


class TestPilotReport(unittest.TestCase):
    def setUp(self):
        self.p = Pilot()

    def tearDown(self):
        self.p.close()

    def make(self, mem1_traffic=300.0, cpu_durations=(60.0, 60.5, 61.0)):
        d = self.p.dir
        cols = ["order", "phase", "round", "position", "part_order"] + benchlib.RESULT_COLUMNS
        rows = []
        for k, dur in enumerate(cpu_durations, 1):
            rows.append(dict(run_id=f"rep{k}_cpu_t8", case_id="cpu_t8", phase="repeat", duration_s=dur, pkg_energy_j=1500,
                             instructions=1e12, cpu_ops_done=360000, cpu_ops_target=360000, dram_read_gib=40, dram_write_gib=20,
                             logging=1, perf_source="summary"))
            rows.append(dict(run_id=f"rep{k}_mem_8g", case_id="mem_8g", phase="repeat", duration_s=60, pkg_energy_j=1000,
                             instructions=5e11, vm_ops_done=2400000, vm_ops_target=2400000, dram_read_gib=180, dram_write_gib=120,
                             logging=1, perf_source="summary"))
            rows.append(dict(run_id=f"rep{k}_mem_1g", case_id="mem_1g", phase="repeat", duration_s=75, pkg_energy_j=1200,
                             instructions=6e11, vm_ops_done=24000000, vm_ops_target=24000000,
                             dram_read_gib=mem1_traffic * 0.6, dram_write_gib=mem1_traffic * 0.4, logging=1, perf_source="summary"))
        rows.append(dict(run_id="idle_start_1", case_id="idle", phase="idle_start", duration_s=60, logging=1, perf_source="summary"))
        for rid, cid in (("r1_cpu_t1", "cpu_t1"), ("r1_mem_2g", "mem_2g"), ("r1_cpu_t2", "cpu_t2")):
            rows.append(dict(run_id=rid, case_id=cid, phase="round", round=1, duration_s=100, settle_s=40, logging=1,
                             perf_source="summary", soft_flags="settle_timeout" if cid == "cpu_t2" else "",
                             dram_read_gib=180 if cid == "mem_2g" else 10, dram_write_gib=120 if cid == "mem_2g" else 5))
        write_table(os.path.join(d, "results_all.csv"), cols, rows)
        att_cols = ["run_id", "case_id", "attempt", "has_window", "t_start_us", "t_end_us", "duration_s", "verdict",
                    "hard_reasons", "soft_flags", "is_the_valid_attempt"]
        write_table(os.path.join(d, "attempts_all.csv"), att_cols, [
            dict(run_id="r1_cpu_t1", attempt="a1", verdict="RETRY", hard_reasons="injected_failure"),
            dict(run_id="r1_cpu_t1", attempt="a2", verdict="VALID", is_the_valid_attempt=1),
            dict(run_id="r1_cpu_t2", attempt="a1", verdict="none"),
            dict(run_id="r1_cpu_t2", attempt="a2", verdict="VALID", is_the_valid_attempt=1)])
        put(os.path.join(d, "recovery_test.txt"), "fail=r1_cpu_t1\nkill=r1_cpu_t2\nkilled_nextflow r1_cpu_t2 2026-11-02T20:00:00Z\n")
        put(os.path.join(d, "progress.txt"), "".join(f"2026-11-02T20:00:00Z {r['run_id']} VALID round=1 pass=main attempts=1 "
                                                      f"nextflow_exit=0 seconds=150\n" for r in rows))
        put(os.path.join(d, "calibration_report.txt"), "CALIBRATION\n  CPU_OPS = 360000\nRESULT: calibration.env written\n")

    def test_good_pilot(self):
        self.make()
        text = pilot_report.report(self.p.dir)
        self.assertIn("injected failed attempt (r1_cpu_t1): PASSED", text)
        self.assertIn("killed Nextflow call (r1_cpu_t2): PASSED", text)
        self.assertIn("cpu_t8: n=3, time 60.5 s (spread 0.8 %)", text)
        self.assertIn("mem_1g: 300.0 GiB (+0.0 % vs 8 GB, n=3)", text)
        self.assertIn("mem_2g: 300.0 GiB (+0.0 % vs 8 GB, n=1)", text)
        self.assertIn("3 of 52 cases valid; one round took 0.12 h", text)
        self.assertIn("totals from: summary", text)
        self.assertIn("soft flags on valid runs: settle_timeout 1", text)
        self.assertIn("rehearsal: 49 case(s) without a valid run", text)

    def test_problems_are_listed(self):
        self.make(mem1_traffic=360.0, cpu_durations=(55.0, 60.0, 66.0))
        os.remove(os.path.join(self.p.dir, "recovery_test.txt"))
        put(os.path.join(self.p.dir, "recovery_test.txt"), "fail=r1_cpu_t1\nkill=r1_mem_2g\n")
        text = pilot_report.report(self.p.dir)
        self.assertIn("repeatability: cpu_t8 time spread 9.1 % (above 5 %)", text)
        self.assertIn("memory sizes: mem_1g moves +20.0 % data compared to 8 GB", text)
        self.assertIn("recovery: the killed Nextflow call of r1_mem_2g was not handled as expected", text)

    def test_main_writes_file(self):
        self.make()
        self.assertEqual(quiet(pilot_report.main, ["pilot_report.py", self.p.dir]), 0)
        self.assertTrue(benchlib.read_text(os.path.join(self.p.dir, "pilot_report.txt")).startswith("PILOT REPORT"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
