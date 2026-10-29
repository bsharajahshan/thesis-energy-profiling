#!/usr/bin/env python3

"""
test_benchlib.py - tests for scripts/bench/benchlib.py (standard library only)

Parsers are tested against real tool output from the test laptop (see
fixtures/README.md). The finish tests build run folders by hand in the same
file formats window.sh and run_case.sh write; test_run_case.sh checks those
formats end to end with the real shell scripts.

Run (Windows or Linux):  python -I scripts/bench/tests/test_benchlib.py
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import benchlib as bl  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


ENV = {
    "STRESS_NG": "/usr/bin/stress-ng", "FIO": "/home/u/.local/bin/fio", "TASKSET": "/usr/bin/taskset",
    "SLEEP_BIN": "/usr/bin/sleep", "RAPL_DRAM_DIR": "/sys/x/dram", "AC_FILE": "/sys/x/ac",
    "CGROUP_OURS": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/x.service",
    "PERF_EVENTS": "instructions,cycles,ref-cycles,msr/tsc/,uncore_imc/data_reads/,uncore_imc/data_writes/,"
                   "power/energy-pkg/,power/energy-ram/,context-switches,cpu-migrations",
    "NCPU": "8", "TSC_HZ": "1992000000",
}


def spec_of(parts, mode="parallel", logging=True, expected=60, **extra):
    s = {"run_id": "t-001", "case_id": "test_case", "mode": mode, "logging": logging,
         "governor": "powersave", "epp": "balance_performance", "expected_s": expected, "parts": parts}
    s.update(extra)
    return s


# ---- parsers on real output -------------------------------------------------

class TestStressNg(unittest.TestCase):
    def test_cpu8_real(self):
        st = bl.parse_stress(fixture("stressng-0.17.06_cpu8_60s.txt"))
        cpu = st["stressors"]["cpu"]
        self.assertEqual(cpu["bogo_ops"], 359270)
        self.assertAlmostEqual(cpu["real_s"], 60.00)
        self.assertAlmostEqual(cpu["usr_s"], 401.73)
        self.assertAlmostEqual(cpu["sys_s"], 0.28)
        self.assertAlmostEqual(cpu["ops_per_s"], 5987.57)
        self.assertEqual((st["passed"], st["failed"], st["skipped"], st["untrustworthy"]), (8, 0, 0, 0))
        self.assertTrue(st["completed"])
        self.assertNotIn("stressor", st["stressors"])

    def test_vm_real(self):
        st = bl.parse_stress(fixture("stressng-0.17.06_vm8g_60s.txt"))
        self.assertEqual(st["stressors"]["vm"]["bogo_ops"], 385123)
        self.assertAlmostEqual(st["stressors"]["vm"]["sys_s"], 19.97)

    def test_cpu_load50_real(self):
        # this file has extra 'cpu: for stable load results...' lines per worker
        st = bl.parse_stress(fixture("stressng-0.17.06_cpu8_load50_60s.txt"))
        self.assertEqual(list(st["stressors"]), ["cpu"])
        self.assertEqual(st["stressors"]["cpu"]["bogo_ops"], 258243)

    def test_old_info_tag_and_failures(self):
        text = ("stress-ng: info:  [123] cpu   1000  10.00  80.00  0.10  100.00  12.48\n"
                "stress-ng: info:  [123] failed: 2\n"
                "stress-ng: info:  [123] unsuccessful run completed in 10.01s\n")
        st = bl.parse_stress(text)
        self.assertEqual(st["stressors"]["cpu"]["bogo_ops"], 1000)
        self.assertEqual(st["failed"], 2)
        self.assertFalse(st["completed"])

    def test_empty(self):
        st = bl.parse_stress("")
        self.assertEqual(st["stressors"], {})
        self.assertIsNone(st["failed"])


class TestFio(unittest.TestCase):
    def test_write_real(self):
        f = bl.parse_fio_json(fixture("fio-3.42_write.json"))
        self.assertEqual(f["version"], "fio-3.42")
        self.assertEqual(f["error"], 0)
        self.assertEqual(f["io_bytes"], 7516196864)
        self.assertAlmostEqual(f["iops"], 28880.042179)
        self.assertEqual(f["runtime_ms"], 63539)
        self.assertEqual(f["disk"], "sdb")

    def test_text_before_json(self):
        text = "note: both iodepth >= 1 and synchronous I/O engine are selected\n" + fixture("fio-3.42_write.json")
        self.assertEqual(bl.parse_fio_json(text)["io_bytes"], 7516196864)

    def test_not_json(self):
        with self.assertRaises(ValueError):
            bl.parse_fio_json("fio: failed to open file\n")


class TestPerf(unittest.TestCase):
    def test_plain_real(self):
        p = bl.parse_perf_csv(fixture("perf-7.0_stat_plain.csv"))
        t = p["totals"]
        self.assertEqual(p["source"], "plain")
        self.assertEqual(t["instructions"]["value"], 1594451560611)
        self.assertEqual(t["cpu-migrations"]["value"], 7481)
        self.assertEqual(t["uncore_imc/data_reads/"]["unit"], "MiB")
        self.assertAlmostEqual(bl.to_gib(t["uncore_imc/data_reads/"]), 175751.00 / 1024)
        self.assertAlmostEqual(t["cycles"]["pct"], 100.0)
        self.assertEqual(p["series"], [])

    def test_interval_with_summary_assumed(self):
        wanted = ENV["PERF_EVENTS"].split(",")
        p = bl.parse_perf_csv(fixture("perf_interval_ASSUMED.csv"), wanted)
        self.assertEqual(p["source"], "summary")
        self.assertEqual(p["totals"]["instructions"]["value"], 5169135780)
        self.assertAlmostEqual(p["totals"]["power/energy-pkg/"]["value"], 50.0)
        self.assertEqual(len(p["series"]), 20)
        self.assertEqual(p["series"][0][:3], (1.001083436, "instructions", 2534567890))

    def test_interval_without_summary_is_summed(self):
        text = "\n".join(l for l in fixture("perf_interval_ASSUMED.csv").splitlines() if not l.startswith("summary"))
        p = bl.parse_perf_csv(text)
        self.assertEqual(p["source"], "intervals")
        self.assertEqual(p["totals"]["instructions"]["value"], 2534567890 + 2634567890)
        self.assertAlmostEqual(p["totals"]["uncore_imc/data_reads/"]["value"], 6000.0)

    def test_summary_without_prefix(self):
        text = ("1.000000000,100,,cycles,1000,100.00,,\n"
                "2.000000000,150,,cycles,1000,100.00,,\n"
                "250,,cycles,2000,100.00,,\n")
        p = bl.parse_perf_csv(text, ["cycles"])
        self.assertEqual(p["source"], "plain")
        self.assertEqual(p["totals"]["cycles"]["value"], 250)
        self.assertEqual(len(p["series"]), 2)

    def test_not_counted_and_multiplexed(self):
        text = ("<not counted>,,ref-cycles,0,0.00,,\n"
                "<not supported>,,msr/tsc/,,,,\n"
                "12345,,cycles,500,50.00,,\n")
        t = bl.parse_perf_csv(text)["totals"]
        self.assertIsNone(t["ref-cycles"]["value"])
        self.assertIsNone(t["msr/tsc/"]["value"])
        self.assertAlmostEqual(t["cycles"]["pct"], 50.0)


class TestSmallParsers(unittest.TestCase):
    def test_to_us(self):
        self.assertEqual(bl.to_us("1761800000.123456"), 1761800000123456)
        self.assertEqual(bl.to_us("1761800000,5"), 1761800000500000)   # German locale
        with self.assertRaises(ValueError):
            bl.to_us("-")

    def test_energy_wrap(self):
        self.assertEqual(bl.energy_delta(100, 300, 1000), 200)
        self.assertEqual(bl.energy_delta(900, 100, 1000), 200)
        self.assertIsNone(bl.energy_delta(900, 100, None))
        self.assertIsNone(bl.energy_delta(None, 100, 1000))

    def test_size(self):
        self.assertEqual(bl.size_to_bytes("8G"), 8 * 1024 ** 3)
        self.assertEqual(bl.size_to_bytes("512M"), 512 * 1024 ** 2)
        self.assertEqual(bl.size_to_bytes("4096"), 4096)
        with self.assertRaises(ValueError):
            bl.size_to_bytes("lots")

    def test_read_env_quoting(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "session.env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# comment\nA='/x y/z'\nexport B=2\nC=\nPERF_EVENTS='a,b/c/'\n")
            env = bl.read_env(path)
            self.assertEqual(env, {"A": "/x y/z", "B": "2", "C": "", "PERF_EVENTS": "a,b/c/"})
            self.assertEqual(bl.setting(env, "SETTLE_MAX_S"), "300")
            self.assertEqual(bl.setting({"SETTLE_MAX_S": ""}, "SETTLE_MAX_S"), "300")
        finally:
            shutil.rmtree(d)


# ---- plan --------------------------------------------------------------------

class TestPlan(unittest.TestCase):
    def build(self, spec, env=ENV, out="/out"):
        return bl.build_plan(spec, dict(env), out)

    def test_cpu_ops_total(self):
        plan, files, sh = self.build(spec_of([{"name": "cpu", "kind": "cpu", "threads": 8, "ops": 360000}]))
        self.assertIn("exec /usr/bin/stress-ng --cpu 8 --cpu-method all --cpu-ops 360000 --timeout 270s --metrics-brief",
                      files["part_0.sh"])
        self.assertEqual(plan["parts"][0]["target"], 360000)
        self.assertIn("PLAN_WATCHDOG_S=300\n", sh)          # max(5 x 60, 60 + 120)
        self.assertIn("PLAN_GOVERNOR=powersave\n", sh)

    def test_cpu_ops_per_instance(self):
        env = dict(ENV, OPS_SEMANTICS="per_instance")
        plan, files, _ = self.build(spec_of([{"kind": "cpu", "threads": 8, "ops": 1001}]), env)
        self.assertIn("--cpu-ops 126 ", files["part_0.sh"])
        self.assertEqual(plan["parts"][0]["target"], 1008)

    def test_partial_load_and_pin(self):
        _, files, _ = self.build(spec_of([{"kind": "cpu", "threads": 7, "fixed_s": 60, "load": 50, "pin": "0-2,4-7"}]))
        self.assertIn("exec /usr/bin/taskset -c 0-2,4-7 /usr/bin/stress-ng --cpu 7 --cpu-method all --cpu-load 50 "
                      "--timeout 60s --metrics-brief", files["part_0.sh"])

    def test_vm_and_memory_need(self):
        parts = [{"kind": "vm", "vm_bytes": "4G", "ops": 100}, {"kind": "vm", "vm_bytes": "4G", "ops": 100}]
        plan, files, _ = self.build(spec_of(parts))
        self.assertIn("--vm 1 --vm-bytes 4G --vm-method all --vm-ops 100", files["part_0.sh"])
        self.assertEqual(plan["need_mem_kb"], 8 * 1024 ** 2 + 1024 ** 2)
        plan, _, _ = self.build(spec_of(parts, mode="sequential"))
        self.assertEqual(plan["need_mem_kb"], 4 * 1024 ** 2 + 1024 ** 2)

    def test_io_scripts(self):
        env = dict(ENV, IO_DIR="/data/io")
        plan, files, sh = self.build(spec_of([{"kind": "io", "pattern": "randwrite", "bytes": 7 * 1024 ** 3}]), env)
        self.assertIn("--filename=/data/io/fio_t-001_p0.dat", files["prep_0.sh"])
        self.assertIn("--create_only=1", files["prep_0.sh"])
        self.assertNotIn("--create_only", files["part_0.sh"])
        self.assertIn("--rw=randwrite", files["part_0.sh"])
        self.assertIn(f"--io_size={7 * 1024 ** 3} --end_fsync=1", files["part_0.sh"])
        self.assertIn("--output-format=json --output=/out/part_0.json", files["part_0.sh"])
        self.assertIn("exec rm -f /data/io/fio_t-001_p0.dat", files["clean_0.sh"])
        self.assertEqual(plan["need_disk_kb"], 7 * 1024 ** 2 + 3 * 1024 ** 2)    # file + 3 GiB kept free

    def test_idle(self):
        _, files, _ = self.build(spec_of([{"kind": "idle", "seconds": 60}]))
        self.assertIn("exec /usr/bin/sleep 60\n", files["part_0.sh"])

    def test_spec_errors(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "spec.json")
            bad = [
                spec_of([{"kind": "cpu", "threads": 8, "ops": 10, "fixed_s": 60}]),
                spec_of([{"kind": "gpu"}]),
                spec_of([{"kind": "io", "pattern": "read", "bytes": 1}]),
                spec_of([{"kind": "vm", "vm_bytes": "lots", "ops": 1}]),
                spec_of([]),
                dict(spec_of([{"kind": "idle", "seconds": 1}]), run_id="a b"),
                dict(spec_of([{"kind": "idle", "seconds": 1}]), mode="both"),
            ]
            for s in bad:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(s, f)
                with self.assertRaises(ValueError, msg=str(s)):
                    bl.load_spec(path)
        finally:
            shutil.rmtree(d)


# ---- finish on hand-made run folders ------------------------------------------

T0 = 1761800000.0


def snap_text(**kw):
    v = dict(pkg=1_000_000, pkg_max=262143328850, dram=500_000, temp=52000, ac=1, gov="powersave",
             epp="balance_performance", thr=10, thr_ms=100, pkg_thr=5, cg_ours=1_000_000, cg_user=2_000_000,
             cg_system=3_000_000, busy=150, sectors=1000, t=T0)
    v.update(kw)
    lines = [f"time_us {v['t']:.6f}", f"pkg_uj {v['pkg']}", f"pkg_max_uj {v['pkg_max']}",
             f"dram_uj {v['dram']}", f"dram_max_uj {v['pkg_max']}", f"temp_mc {v['temp']}", f"ac {v['ac']}"]
    for c in ("cpu0", "cpu1"):
        lines += [f"gov {c} {v['gov']}", f"epp {c} {v['epp']}", f"thr_core {c} {v['thr']} {v['thr_ms']}"]
    lines += [f"thr_pkg_count {v['pkg_thr']}", "thr_pkg_ms 50",
              "lim pkg 0 long_term 200000000", "lim pkg 1 short_term 44000000",
              "lim mmio 0 long_term 15000000", "lim mmio 1 short_term 25000000",
              f"cg ours {v['cg_ours']}", f"cg user {v['cg_user']}", f"cg system {v['cg_system']}", "cg init 10000",
              f"procstat cpu  {v['busy']} 0 0 1000 5 0 0 0 0 0",
              f"disk 100 0 200 300 400 0 {v['sectors']} 500 0 600 900 0 0 0 0 0 0",
              "memavail_kb 13000000"]
    return "\n".join(lines) + "\n"


def stress_text(name, ops, real=60.0):
    return (f"stress-ng: info:  [100] dispatching hogs: 8 {name}\n"
            f"stress-ng: metrc: [100] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s\n"
            f"stress-ng: metrc: [100]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)\n"
            f"stress-ng: metrc: [100] {name:<12} {ops:>10} {real:9.2f} {real * 7:9.2f}      0.28 {ops / real:12.2f} {ops / real / 7:14.2f}\n"
            "stress-ng: info:  [100] skipped: 0\nstress-ng: info:  [100] passed: 8: x (8)\n"
            "stress-ng: info:  [100] failed: 0\nstress-ng: info:  [100] metrics untrustworthy: 0\n"
            "stress-ng: info:  [100] successful run completed in 1 min, 0.01 secs\n")


class FakeRun:
    """A run folder as window.sh / run_case.sh would leave it (energy 1500 J pkg, 60 J DRAM, 60 s)."""

    def __init__(self, spec, env=ENV):
        self.dir = tempfile.mkdtemp()
        self.env = dict(env)
        self.plan, files, _ = bl.build_plan(spec, self.env, self.dir)
        for name, text in files.items():
            self.write(name, text)
        self.write("plan.json", json.dumps(self.plan))
        self.write("snap_before.txt", snap_text())
        self.write("snap_after.txt", snap_text(pkg=1_501_000_000, dram=60_500_000, thr=12, thr_ms=150, pkg_thr=6,
                                               cg_ours=400_000_000, cg_user=401_500_000, cg_system=3_500_000,
                                               busy=48_150, sectors=1000 + 2 * 1024 ** 2, temp=96000, t=T0 + 60))
        self.write("window.txt", f"win_start {T0:.6f} 1000000 500000\nwin_end {T0 + 60:.6f} 1501000000 60500000\n")
        self.write("settle.txt", "settle_s 42\ntemp_c 54\n")
        for pp in self.plan["parts"]:
            i = pp["index"]
            self.write(f"part_{i}.time", f"{T0 + 0.001:.6f} {T0 + 59.9:.6f} 0\n")
            if pp["kind"] in ("cpu", "vm"):
                ops = pp["target"] if pp["target_unit"] == "ops" else 12345
                self.write(f"part_{i}.out", stress_text(pp["kind"], ops))
            elif pp["kind"] == "io":
                data = json.loads(fixture("fio-3.42_write.json"))
                data["jobs"][0]["write"]["io_bytes"] = pp["target"]
                self.write(f"part_{i}.json", json.dumps(data))
        if self.plan["logging"]:
            self.write("perf.csv", fixture("perf_interval_ASSUMED.csv"))
            self.write("sampler.txt", "".join(f"{T0 + s:.6f} {1_000_000 + s * 25_000_000} {500_000 + s * 1_000_000} "
                                              f"{60000 + s * 500}\n" for s in range(-5, 62)))

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def finish(self, attempt=1, max_attempts=3):
        return bl.evaluate(self.plan, self.dir, self.env, attempt, max_attempts)

    def close(self):
        shutil.rmtree(self.dir)


CPU_VM = [{"name": "cpu", "kind": "cpu", "threads": 8, "ops": 360000},
          {"name": "ram", "kind": "vm", "vm_bytes": "8G", "ops": 2000}]


class TestFinish(unittest.TestCase):
    def run_fake(self, spec, change=None, attempt=1, max_attempts=3, env=ENV):
        fr = FakeRun(spec, env)
        try:
            if change:
                change(fr)
            return fr.finish(attempt, max_attempts)
        finally:
            fr.close()

    def test_valid_parallel(self):
        row, parts, hard, soft = self.run_fake(spec_of(CPU_VM, round=2, block="rq2"))
        self.assertEqual(hard, [], soft)
        self.assertTrue(row["valid"])
        self.assertAlmostEqual(row["duration_s"], 60.0)
        self.assertAlmostEqual(row["pkg_energy_j"], 1500.0)
        self.assertAlmostEqual(row["dram_energy_j"], 60.0)
        self.assertAlmostEqual(row["pkg_power_w"], 25.0)
        self.assertEqual((row["cpu_ops_target"], row["cpu_ops_done"]), (360000, 360000))
        self.assertEqual((row["vm_ops_target"], row["vm_ops_done"]), (2000, 2000))
        self.assertEqual((row["round"], row["block"]), (2, "rq2"))
        self.assertEqual(row["instructions"], 5169135780)
        self.assertAlmostEqual(row["busy_mhz"], 4402469134 / 3680246912 * 1992.0)
        self.assertAlmostEqual(row["busy_cpus"], 3680246912 / 1.992e9 / 60)
        self.assertAlmostEqual(row["dram_read_gib"], 6000 / 1024)
        self.assertAlmostEqual(row["perf_pkg_energy_j"], 50.0)
        self.assertEqual(row["perf_source"], "summary")
        self.assertAlmostEqual(row["temp_start_c"], 52.0)
        self.assertAlmostEqual(row["temp_max_c"], 90.0)
        self.assertAlmostEqual(row["pkg_power_max_1s_w"], 25.0)
        self.assertEqual((row["thr_core_count"], row["thr_core_ms"], row["thr_pkg_count"]), (4, 100, 1))
        self.assertEqual((row["pl1_w_start"], row["pl2_w_start"], row["mmio_pl1_w_start"]), (200.0, 44.0, 15.0))
        self.assertEqual((row["governor_start"], row["epp_end"]), ("powersave", "balance_performance"))
        self.assertEqual(row["disk_written_bytes"], 2 * 1024 ** 2 * 512)
        self.assertAlmostEqual(row["cpu_ours_s"], 399.0)
        self.assertAlmostEqual(row["cpu_other_s"], (401.5 - 2 - 399.0) + 0.5)   # other user + system
        self.assertAlmostEqual(row["cpu_total_busy_s"], 480.0)
        self.assertEqual(row["settle_s"], 42)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(p["complete"] for p in parts))

    def test_work_incomplete(self):
        def short(fr):
            fr.write("part_0.out", stress_text("cpu", 359999))
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), short)
        self.assertIn("work_incomplete_p0", hard)

    def test_missing_counters_and_failed_part(self):
        def broken(fr):
            fr.write("part_1.out", "stress-ng: error: [1] out of memory\n")
            fr.write("part_0.time", f"{T0:.6f} {T0 + 1:.6f} 2\n")
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), broken)
        self.assertIn("work_incomplete_p1", hard)
        self.assertIn("part_exit_2_p0", hard)

    def test_wraparound(self):
        def wrap(fr):
            top = 262143328850
            fr.write("window.txt", f"win_start {T0:.6f} {top - 500_000_000} 500000\n"
                                   f"win_end {T0 + 60:.6f} 1000000000 60500000\n")
        row, _, hard, _ = self.run_fake(spec_of(CPU_VM), wrap)
        self.assertAlmostEqual(row["pkg_energy_j"], 1500.0)
        self.assertEqual(hard, [])

    def test_governor_not_as_planned(self):
        def perf_gov(fr):
            fr.write("snap_before.txt", snap_text(gov="performance"))
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), perf_gov)
        self.assertIn("governor_not_as_planned", hard)

    def test_disturbed_retry_then_kept(self):
        def busy(fr):   # other programs used 30 s of CPU in 60 s
            fr.write("snap_after.txt", snap_text(pkg=1_501_000_000, dram=60_500_000, cg_ours=400_000_000,
                                                 cg_user=401_500_000, cg_system=33_000_000, t=T0 + 60))
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), busy, attempt=1, max_attempts=3)
        self.assertIn("disturbed", hard)
        _, _, hard, soft = self.run_fake(spec_of(CPU_VM), busy, attempt=3, max_attempts=3)
        self.assertEqual(hard, [])
        self.assertIn("disturbed_kept_last_attempt", soft)

    def test_on_battery(self):
        def unplug(fr):
            fr.write("snap_after.txt", snap_text(pkg=1_501_000_000, dram=60_500_000, ac=0, t=T0 + 60))
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), unplug)
        self.assertIn("on_battery", hard)

    def test_dram_missing(self):
        def nodram(fr):
            fr.write("window.txt", f"win_start {T0:.6f} 1000000 500000\nwin_end {T0 + 60:.6f} 1501000000 -\n")
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), nodram)
        self.assertIn("dram_energy_missing", hard)

    def test_lock_busy_only(self):
        def busy_lock(fr):
            os.remove(os.path.join(fr.dir, "window.txt"))
            fr.write("events.txt", "lock_busy\n")
        _, _, hard, _ = self.run_fake(spec_of(CPU_VM), busy_lock)
        self.assertIn("lock_busy", hard)
        self.assertNotIn("window_incomplete", hard)

    def test_watchdog_and_settle_events(self):
        def ev(fr):
            fr.write("events.txt", "settle_timeout 71\nwatchdog_fired 1761800300.0\n")
        row, _, hard, soft = self.run_fake(spec_of(CPU_VM), ev)
        self.assertIn("watchdog_fired", hard)
        self.assertIn("settle_timeout", soft)
        self.assertTrue(row["settle_timeout"])

    def test_perf_missing_is_soft(self):
        def noperf(fr):
            os.remove(os.path.join(fr.dir, "perf.csv"))
        row, _, hard, soft = self.run_fake(spec_of(CPU_VM), noperf)
        self.assertEqual(hard, [])
        self.assertIn("perf_missing", soft)
        self.assertIsNone(row["instructions"])

    def test_sequential_subwindows(self):
        def subs(fr):
            fr.write("window.txt", f"win_start {T0:.6f} 1000000 500000\n"
                                   f"sub_start 0 {T0:.6f} 1000000 500000\n"
                                   f"sub_end 0 {T0 + 30:.6f} 901000000 30500000\n"
                                   f"sub_start 1 {T0 + 30:.6f} 901000000 30500000\n"
                                   f"sub_end 1 {T0 + 60:.6f} 1501000000 60500000\n"
                                   f"win_end {T0 + 60:.6f} 1501000000 60500000\n")
        _, parts, hard, _ = self.run_fake(spec_of(CPU_VM, mode="sequential"), subs)
        self.assertEqual(hard, [])
        self.assertAlmostEqual(parts[0]["sub_pkg_energy_j"], 900.0)
        self.assertAlmostEqual(parts[1]["sub_pkg_energy_j"], 600.0)
        self.assertAlmostEqual(parts[1]["sub_dram_energy_j"], 30.0)

    def test_io_idle_and_fixed_time(self):
        parts = [{"kind": "io", "pattern": "write", "bytes": 7516196864},
                 {"kind": "idle", "seconds": 60},
                 {"kind": "cpu", "threads": 4, "fixed_s": 60}]
        row, prow, hard, _ = self.run_fake(spec_of(parts, logging=False))
        self.assertEqual(hard, [])
        self.assertEqual((row["io_bytes_target"], row["io_bytes_done"]), (7516196864, 7516196864))
        self.assertIsNone(row["cpu_ops_target"])
        self.assertEqual(row["cpu_ops_done"], 12345)
        self.assertAlmostEqual(prow[1]["done"], 59.899, places=3)

    def test_fixed_time_too_short(self):
        def cut(fr):
            fr.write("part_0.time", f"{T0:.6f} {T0 + 30:.6f} 0\n")
        _, _, hard, _ = self.run_fake(spec_of([{"kind": "cpu", "threads": 4, "fixed_s": 60}], logging=False), cut)
        self.assertIn("work_incomplete_p0", hard)

    def test_cmd_finish_writes_files(self):
        fr = FakeRun(spec_of(CPU_VM))
        try:
            env_path = os.path.join(fr.dir, "session.env")
            fr.write(env_path, "".join(f"{k}='{v}'\n" for k, v in ENV.items()))
            rc = bl.cmd_finish("unused", env_path, fr.dir, "1", "3")
            self.assertEqual(rc, bl.EXIT_VALID)
            with open(os.path.join(fr.dir, "verdict.txt"), encoding="utf-8") as f:
                self.assertEqual(f.readline().strip(), "VALID")
            with open(os.path.join(fr.dir, "result.csv"), encoding="utf-8") as f:
                header, line = f.read().splitlines()
            self.assertEqual(header.split(","), bl.RESULT_COLUMNS)
            self.assertEqual(len(line.split(",")), len(bl.RESULT_COLUMNS))
            self.assertTrue(re.search(r",1500,", line))
        finally:
            fr.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
