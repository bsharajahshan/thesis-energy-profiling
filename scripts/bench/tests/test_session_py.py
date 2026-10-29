#!/usr/bin/env python3

"""
test_session_py.py - tests for preflight.py (discovery in a fake /sys) and sessionlib.py

The whole start/stop/resume behaviour is tested by test_session.sh (bash, real
Nextflow); these tests cover the parts that are pure Python, on any OS.

Run:  python -I scripts/bench/tests/test_session_py.py
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, BENCH)
import preflight  # noqa: E402
import sessionlib  # noqa: E402

CAL = os.path.join(BENCH, "calibration.env")


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{text}\n")


def fake_sys(root, smt=True):
    """The parts of /sys and /proc that preflight.py reads, as on the i7-8550U laptop."""
    pkg = os.path.join(root, "sys/class/powercap/intel-rapl:0")
    put(f"{pkg}/name", "package-0")
    put(f"{pkg}/energy_uj", 123)
    put(f"{pkg}/constraint_0_name", "long_term")
    put(f"{pkg}/constraint_0_power_limit_uw", 200000000)
    put(f"{pkg}/intel-rapl:0:0/name", "core")
    put(f"{pkg}/intel-rapl:0:2/name", "dram")
    put(f"{pkg}/intel-rapl:0:2/energy_uj", 45)
    put(os.path.join(root, "sys/class/hwmon/hwmon1/name"), "acpitz")
    put(os.path.join(root, "sys/class/hwmon/hwmon5/name"), "coretemp")
    put(os.path.join(root, "sys/class/hwmon/hwmon5/temp1_label"), "Package id 0")
    put(os.path.join(root, "sys/class/hwmon/hwmon5/temp1_input"), 51000)
    put(os.path.join(root, "sys/class/power_supply/ADP1/type"), "Mains")
    put(os.path.join(root, "sys/class/power_supply/ADP1/online"), 1)
    put(os.path.join(root, "sys/class/power_supply/BAT0/type"), "Battery")
    put(os.path.join(root, "sys/class/power_supply/BAT0/capacity"), 77)
    cpu = os.path.join(root, "sys/devices/system/cpu")
    put(f"{cpu}/online", "0-7")
    put(f"{cpu}/intel_pstate/status", "active")
    put(f"{cpu}/intel_pstate/no_turbo", 0)
    for c in range(8):
        put(f"{cpu}/cpu{c}/topology/thread_siblings_list", f"{c % 4},{c % 4 + 4}" if smt else str(c))
        put(f"{cpu}/cpu{c}/cpufreq/scaling_governor", "powersave")
        put(f"{cpu}/cpu{c}/cpufreq/energy_performance_preference", "balance_performance")
        put(f"{cpu}/cpu{c}/cpufreq/scaling_available_governors", "performance powersave")
        put(f"{cpu}/cpu{c}/cpufreq/energy_performance_available_preferences",
            "default performance balance_performance balance_power power")
    put(f"{cpu}/cpu0/thermal_throttle/core_throttle_count", 0)
    put(os.path.join(root, "sys/fs/cgroup/cgroup.controllers"), "cpu memory")
    put(os.path.join(root, "proc/meminfo"), "MemTotal:       16000000 kB\nMemAvailable:   12000000 kB")
    put(os.path.join(root, "proc/self/cgroup"), "0::/user.slice/user-1000.slice/user@1000.service/app.slice/thesis-bench-main.service")


@unittest.skipIf(os.name == "nt", "folder names with ':' (intel-rapl:0) are not possible on Windows; run in WSL/Linux")
class Preflight(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.old = preflight.SYSROOT
        preflight.SYSROOT = self.root
        self.rep = preflight.Report()
        self.env = {}
        self.cal = preflight.cases.load_calibration(CAL)

    def tearDown(self):
        preflight.SYSROOT = self.old
        shutil.rmtree(self.root)

    def levels(self, topic):
        return [l.split()[0] for l in self.rep.lines if l.split()[1] == topic]

    def test_good_machine(self):
        fake_sys(self.root)
        rows = [{"case_id": "mech_pin_shared_core"}, {"case_id": "gov_cpu_t1"}]
        preflight.check_rapl(self.rep, self.env)
        preflight.check_temperature(self.rep, self.env)
        preflight.check_power(self.rep, self.env, {}, self.cal)
        preflight.check_cpu(self.rep, self.env, self.cal, rows)
        preflight.check_memory(self.rep, self.env, self.cal)
        preflight.check_cgroup(self.rep, self.env)
        self.assertEqual(self.rep.fails, 0, "\n".join(self.rep.lines))
        self.assertTrue(self.env["RAPL_DRAM_DIR"].endswith("intel-rapl:0:2"))
        self.assertTrue(self.env["TEMP_FILE"].endswith(os.path.join("hwmon5", "temp1_input")))
        self.assertTrue(self.env["AC_FILE"].endswith(os.path.join("ADP1", "online")))
        self.assertTrue(self.env["BATTERY_FILE"].endswith(os.path.join("BAT0", "capacity")))
        self.assertEqual(self.env["CPU_SIBLINGS"], "0,4;1,5;2,6;3,7")
        self.assertEqual(self.env["NCPU"], "8")
        self.assertIn("200 W", "\n".join(self.rep.lines))

    def test_rapl_unreadable_and_no_dram(self):
        fake_sys(self.root)
        os.remove(os.path.join(self.root, "sys/class/powercap/intel-rapl:0/intel-rapl:0:2/name"))
        put(os.path.join(self.root, "sys/class/powercap/intel-rapl:0/energy_uj"), "not a number")
        preflight.check_rapl(self.rep, self.env)
        self.assertEqual(self.levels("rapl")[0], "FAIL")
        text = "\n".join(self.rep.lines)
        self.assertIn("package energy_uj not readable", text)
        self.assertIn("no DRAM domain", text)
        self.assertIn("setup_root.sh", text)

    def test_power_cable_out(self):
        fake_sys(self.root)
        put(os.path.join(self.root, "sys/class/power_supply/ADP1/online"), 0)
        preflight.check_power(self.rep, self.env, {}, self.cal)
        self.assertIn("FAIL", self.levels("power"))

    def test_no_smt_and_governor_not_writable(self):
        fake_sys(self.root, smt=False)
        rows = [{"case_id": "mech_pin_shared_core"}, {"case_id": "gov_cpu_t1"}]
        gov = os.path.join(self.root, "sys/devices/system/cpu/cpu3/cpufreq/scaling_governor")
        if os.geteuid() == 0:
            self.skipTest("root can write read-only files")
        os.chmod(gov, 0o444)      # like the real file before the one-time setup (root-owned 0644)
        preflight.check_cpu(self.rep, self.env, self.cal, rows)
        self.assertIn("FAIL", self.levels("cpu"))
        self.assertIn("FAIL", self.levels("governor"))

    def test_no_temperature(self):
        fake_sys(self.root)
        put(os.path.join(self.root, "sys/class/hwmon/hwmon5/temp1_label"), "Core 0")
        preflight.check_temperature(self.rep, self.env)
        self.assertEqual(self.levels("temperature"), ["FAIL"])

    def test_memory_too_small(self):
        fake_sys(self.root)
        put(os.path.join(self.root, "proc/meminfo"), "MemTotal:        8000000 kB\nMemAvailable:    7000000 kB")
        preflight.check_memory(self.rep, self.env, self.cal)
        self.assertEqual(self.levels("memory"), ["FAIL"])

    def test_session_env_quoting(self):
        path = os.path.join(self.root, "session.env")
        preflight.write_session_env(path, {"A": "x y", "WAIT_FOR_GONE": "opencode hermes", "B": "it's"})
        env = preflight.benchlib.read_env(path)
        self.assertEqual(env, {"A": "x y", "WAIT_FOR_GONE": "opencode hermes", "B": "it's"})

    def test_cgroup_dir(self):
        fake_sys(self.root)
        old = sessionlib.SYSROOT
        sessionlib.SYSROOT = self.root
        try:
            self.assertEqual(sessionlib.cgroup_dir(), self.root + "/sys/fs/cgroup/user.slice/user-1000.slice/"
                                                     "user@1000.service/app.slice/thesis-bench-main.service")
        finally:
            sessionlib.SYSROOT = old


SCHED = """# test
order,run_id,phase,round,position,case_id,part_order
1,idle_start_1,idle_start,,1,idle,
2,r1_cpu_t8,round,1,1,cpu_t8,
3,r1_mem_1g,round,1,2,mem_1g,
4,r2_cpu_t8,round,2,1,cpu_t8,
5,idle_end_1,idle_end,,1,idle,
"""


class Session(unittest.TestCase):
    def setUp(self):
        self.res = tempfile.mkdtemp()
        self.ses = os.path.join(self.res, "main")
        put(os.path.join(self.ses, "schedule.csv"), SCHED)
        shutil.copyfile(CAL, os.path.join(self.ses, "calibration.env"))
        put(os.path.join(self.ses, "preflight.txt"), "OK    tools  fine\nWARN  programs  running: opencode")
        put(os.path.join(self.ses, "first_start.txt"), "2026-11-04T19:00:00Z")
        self.done("idle_start_1")
        self.done("r1_cpu_t8", flags="settle_timeout, perf_multiplexed:cycles", attempts=2)
        self.failed("r1_mem_1g", "work_incomplete_p0", attempts=3)

    def tearDown(self):
        shutil.rmtree(self.res)

    def done(self, run_id, flags="-", attempts=1):
        for k in range(1, attempts + 1):
            put(os.path.join(self.ses, "runs", run_id, f"a{k}", "verdict.txt"),
                ("VALID\nhard: -\nsoft: " + flags) if k == attempts else "RETRY\nhard: disturbed\nsoft: -")
        put(os.path.join(self.ses, "runs", run_id, "VALID"), f"a{attempts}")

    def failed(self, run_id, reason, attempts):
        for k in range(1, attempts + 1):
            put(os.path.join(self.ses, "runs", run_id, f"a{k}", "verdict.txt"), f"RETRY\nhard: {reason}\nsoft: -")

    def pending(self, which):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sessionlib.cmd_pending(self.ses, which)
        return buf.getvalue().split("\n")[:-1]

    def test_pending(self):
        self.assertEqual(self.pending("all"), ["r1_mem_1g round 1", "r2_cpu_t8 round 2", "idle_end_1 idle_end -"])
        self.assertEqual(self.pending("except:probe,idle_end"), ["r1_mem_1g round 1", "r2_cpu_t8 round 2"])
        self.assertEqual(self.pending("only:idle_end"), ["idle_end_1 idle_end -"])
        self.assertEqual(self.pending("only:probe"), [])
        with self.assertRaises(ValueError):
            sessionlib.phase_filter("idle_end")

    def test_summary_missing(self):
        text = sessionlib.summary_text(self.ses, "main")
        lines = text.splitlines()
        self.assertEqual(lines[0], "MISSING: 3 of 5 runs: r1_mem_1g r2_cpu_t8 idle_end_1")
        self.assertIn("runs valid: 2 of 5", text)
        self.assertIn("attempts: 6 in total; 2 runs needed more than one", text)
        self.assertIn("  settle_timeout: 1 run(s): r1_cpu_t8", text)
        self.assertIn("  perf_multiplexed: 1 run(s): r1_cpu_t8", text)
        self.assertIn("  r1_mem_1g: work_incomplete_p0", text)
        self.assertIn("  r2_cpu_t8: never started", text)
        self.assertIn("WARN  programs  running: opencode", text)
        self.assertIn(os.path.join(self.res, "thesis_main.tar.gz"), text)

    def test_summary_done_and_stopped(self):
        for r in ("r1_mem_1g", "r2_cpu_t8", "idle_end_1"):
            self.done(r)
        self.assertEqual(sessionlib.summary_text(self.ses, "main").splitlines()[0], "DONE")
        stopped = sessionlib.summary_text(self.ses, "main", "no power cable")
        self.assertTrue(stopped.startswith("STOPPED: no power cable. Start the same command again"))

    def test_status(self):
        put(os.path.join(self.ses, "state.txt"), "status=running\nupdated=2026-11-04T20:00:00Z\ncurrent=r1_mem_1g (main)")
        text = sessionlib.status_text(self.res)
        self.assertIn("main: RUNNING (last update 2026-11-04T20:00:00Z, now: r1_mem_1g (main))", text)
        self.assertIn("runs valid: 2 of 5", text)
        self.assertIn("ends about", text)
        put(os.path.join(self.ses, "state.txt"), "status=done")
        put(os.path.join(self.ses, "summary.txt"), "DONE\n...")
        put(os.path.join(self.res, "thesis_main.tar.gz"), "x")
        text = sessionlib.status_text(self.res)
        self.assertIn("main: DONE", text)
        self.assertIn("send this file: " + os.path.join(self.res, "thesis_main.tar.gz"), text)
        self.assertEqual(sessionlib.status_text(os.path.join(self.res, "nothing")), "no session yet\n")

    def test_estimate_and_timeout(self):
        cal = preflight.cases.load_calibration(CAL)
        left = sessionlib.estimate(self.ses)
        per_run = float(cal["SETTLE_EST_S"]) + float(cal["NXF_START_S"])
        expected = (float(cal["VM_S"]) + float(cal["CPU_S_T8"]) + float(cal["IDLE_S"]) + 3 * per_run
                    + 2 * float(cal["BREAK_S"]))     # rounds 1 and 2 plus the idle end: two breaks
        self.assertAlmostEqual(left, expected)
        spec = os.path.join(self.res, "spec.json")
        put(spec, '{"run_id": "x", "case_id": "cpu_t1", "mode": "parallel", "expected_s": 270,'
                  ' "parts": [{"kind": "idle", "seconds": 1}]}')
        self.assertEqual(sessionlib.call_timeout(spec, 3), 3 * (1350 + 300 + 600 + 120) + 300)


if __name__ == "__main__":
    unittest.main(verbosity=1)
