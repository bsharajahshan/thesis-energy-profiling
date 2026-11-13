#!/usr/bin/env python3

"""
test_common.py - tests for scripts/analysis/rerun/common.py

The statistics are checked against scipy and against values worked out by
hand; the data helpers against a tiny made-up runs table. No real data needed.

Run:  py scripts/analysis/rerun/test_common.py
"""

import math
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def runs_table():
    rows = []
    for i in range(1, 6):
        rows.append({"run_id": f"idle_start_{i}", "case_id": "idle", "rep": i, "duration_s": 60.0,
                     "pkg_energy_j": 60.0 * 1.5, "dram_energy_j": 60.0 * 0.3,
                     "pkg_power_w": 1.5, "dram_power_w": 0.3})
        rows.append({"run_id": f"idle_end_{i}", "case_id": "idle", "rep": i, "duration_s": 60.0,
                     "pkg_energy_j": 60.0 * 1.3, "dram_energy_j": 60.0 * 0.3,
                     "pkg_power_w": 1.3, "dram_power_w": 0.3})
    for i, e in zip((3, 1, 2), (1030.0, 1010.0, 1020.0)):
        rows.append({"run_id": f"r{i}_cpu_t8", "case_id": "cpu_t8", "rep": i, "duration_s": 50.0,
                     "pkg_energy_j": e, "dram_energy_j": 20.0,
                     "pkg_power_w": e / 50.0, "dram_power_w": 0.4})
    return pd.DataFrame(rows)


class Statistics(unittest.TestCase):

    def test_welch_matches_scipy(self):
        ref = [1574.1, 1580.2, 1569.8, 1577.0, 1573.4]
        other = [1560.3, 1566.9, 1549.0, 1571.2, 1555.6]
        w = common.welch(ref, other)
        r = sp_stats.ttest_ind(other, ref, equal_var=False)
        ci = r.confidence_interval(0.95)
        self.assertAlmostEqual(w["t"], r.statistic, places=10)
        self.assertAlmostEqual(w["p_welch"], r.pvalue, places=12)
        self.assertAlmostEqual(w["df"], r.df, places=10)
        self.assertAlmostEqual(w["diff_lo"], ci.low, places=8)
        self.assertAlmostEqual(w["diff_hi"], ci.high, places=8)
        self.assertAlmostEqual(w["diff"], np.mean(other) - np.mean(ref), places=10)

    def test_welch_percent_and_sd(self):
        w = common.welch([100.0, 102.0, 98.0], [110.0, 112.0, 108.0])
        self.assertAlmostEqual(w["diff_pct"], 10.0)
        self.assertAlmostEqual(w["sd_ref"], 2.0)
        self.assertAlmostEqual(w["diff_pct_lo"], w["diff_lo"])  # ref mean is 100
        self.assertLess(w["diff_pct_lo"], 10.0)
        self.assertGreater(w["diff_pct_hi"], 10.0)

    def test_welch_without_spread(self):
        w = common.welch([60.0] * 5, [60.0] * 5)
        self.assertTrue(math.isnan(w["p_welch"]))
        self.assertTrue(math.isnan(w["diff_lo"]))
        self.assertEqual(w["diff"], 0.0)

    def test_mann_whitney_floor(self):
        # completely separated groups, 5 vs 5: p = 2 / C(10, 5) = 2/252
        self.assertAlmostEqual(common.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10]), 2 / 252)
        self.assertAlmostEqual(common.mann_whitney([6, 7, 8, 9, 10], [1, 2, 3, 4, 5]), 2 / 252)
        self.assertAlmostEqual(common.mann_whitney([1, 3, 5, 7, 9], [2, 4, 6, 8, 10]),
                               sp_stats.mannwhitneyu([2, 4, 6, 8, 10], [1, 3, 5, 7, 9],
                                                     method="exact").pvalue)

    def test_holm_by_hand(self):
        # sorted 0.005 0.01 0.03 0.04 -> x4 0.02, x3 0.03, x2 0.06, x1 0.04 -> kept at 0.06
        got = common.holm([0.01, 0.04, 0.03, 0.005])
        np.testing.assert_allclose(got, [0.03, 0.06, 0.06, 0.02])

    def test_holm_cap_ties_and_nan(self):
        got = common.holm([0.5, math.nan, 0.5, 0.001])
        self.assertTrue(math.isnan(got[1]))
        np.testing.assert_allclose(got[[0, 2, 3]], [1.0, 1.0, 0.003])
        np.testing.assert_allclose(common.holm([0.02, 0.02]), [0.04, 0.04])

    def test_finish_tests_holm_per_family(self):
        rows = [{"family": "rq1", "p_welch": 0.01}, {"family": "rq1", "p_welch": 0.04},
                {"family": "rq2", "p_welch": 0.04}, {"family": "rq2", "p_welch": math.nan}]
        df = common.finish_tests(rows)
        np.testing.assert_allclose(df["p_holm"].iloc[:3], [0.02, 0.04, 0.04])
        self.assertEqual(list(df["m_family"]), [2, 2, 1, 1])
        self.assertEqual(list(df["holm_ok"].iloc[:3]), [True, True, True])
        self.assertFalse(df["holm_ok"].iloc[3])

    def test_cv(self):
        self.assertAlmostEqual(common.cv_pct([98.0, 100.0, 102.0]), 2.0)


class Data(unittest.TestCase):

    def setUp(self):
        self.runs = runs_table()

    def test_values_in_rep_order(self):
        np.testing.assert_allclose(common.values(self.runs, "cpu_t8", "pkg_energy_j"), [1010, 1020, 1030])
        with self.assertRaises(KeyError):
            common.values(self.runs, "nope", "pkg_energy_j")

    def test_by_rep(self):
        s = common.by_rep(self.runs, "cpu_t8", "pkg_energy_j")
        self.assertEqual(list(s.index), [1, 2, 3])
        self.assertEqual(s.loc[3], 1030.0)
        with self.assertRaises(ValueError):
            common.by_rep(self.runs, "idle", "pkg_energy_j")

    def test_idle_reference(self):
        ref = common.idle_reference(self.runs)
        self.assertEqual((ref["n"], ref["n_start"], ref["n_end"]), (10, 5, 5))
        self.assertAlmostEqual(ref["pkg_w"], 1.4)
        self.assertAlmostEqual(ref["pkg_w_start"], 1.5)
        self.assertAlmostEqual(ref["pkg_w_end"], 1.3)
        self.assertAlmostEqual(ref["dram_w"], 0.3)

    def test_add_active(self):
        ref = common.idle_reference(self.runs)
        out = common.add_active(self.runs, ref)
        row = out[out.run_id == "r1_cpu_t8"].iloc[0]
        self.assertAlmostEqual(row["pkg_idle_j"], 70.0)
        self.assertAlmostEqual(row["pkg_active_j"], 940.0)
        self.assertAlmostEqual(row["dram_idle_j"], 15.0)
        self.assertAlmostEqual(row["dram_active_j"], 5.0)
        np.testing.assert_allclose(out["pkg_idle_j"] + out["pkg_active_j"], out["pkg_energy_j"])
        self.assertNotIn("pkg_idle_j", self.runs.columns)

    def test_summary(self):
        s = common.summary(self.runs, ["pkg_energy_j"]).set_index("case_id")
        self.assertEqual(s.loc["cpu_t8", "n"], 3)
        self.assertAlmostEqual(s.loc["cpu_t8", "pkg_energy_j_mean"], 1020.0)
        self.assertAlmostEqual(s.loc["cpu_t8", "pkg_energy_j_sd"], 10.0)
        self.assertAlmostEqual(s.loc["cpu_t8", "pkg_energy_j_cv_pct"], 100 * 10 / 1020)

    def test_compare_cases(self):
        rows = common.compare_cases(self.runs, "rq1", "x", "cpu_t8", "cpu_t8", ["pkg_energy_j", "duration_s"])
        self.assertEqual([r["metric"] for r in rows], ["pkg_energy_j", "duration_s"])
        self.assertEqual(rows[0]["diff"], 0.0)
        self.assertTrue(math.isnan(rows[1]["p_welch"]))  # durations have no spread

    def test_perf_seconds(self):
        # 2 GHz TSC, 8 CPUs; second 1: 4 CPUs busy at 3 GHz; second 2 (0.5 s): almost idle
        tsc = 2e9
        rows = []
        for t, dt, busy, ghz in ((1.0, 1.0, 4.0, 3.0), (1.5, 0.5, 0.05, 1.0)):
            ref = busy * tsc * dt
            for ev, v in (("msr/tsc/", 8 * tsc * dt), ("ref-cycles", ref), ("cycles", ref * ghz * 1e9 / tsc),
                          ("instructions", 2 * ref * ghz * 1e9 / tsc), ("uncore_imc/data_reads/", 1024 * dt),
                          ("uncore_imc/data_writes/", 2048 * dt), ("power/energy-pkg/", 20 * dt)):
                rows.append({"run_id": "r", "t_s": t, "event": ev, "value": v})
        s = common.perf_seconds(pd.DataFrame(rows))
        np.testing.assert_allclose(s["dt"], [1.0, 0.5])
        np.testing.assert_allclose(s["busy_cpus"], [4.0, 0.05])
        self.assertAlmostEqual(s["clock_mhz"].iloc[0], 3000.0)
        self.assertTrue(math.isnan(s["clock_mhz"].iloc[1]))
        np.testing.assert_allclose(s["ipc"], [2.0, 2.0])
        np.testing.assert_allclose(s["dram_gib_s"], [3.0, 3.0])
        np.testing.assert_allclose(s["pkg_w"], [20.0, 20.0])

    def test_dram_model(self):
        rng = np.random.default_rng(1)
        t = rng.uniform(50, 250, 40)
        gib = rng.uniform(0, 1500, 40)
        runs = pd.DataFrame({"case_id": ["x"] * 40, "duration_s": t, "dram_read_gib": gib / 2,
                             "dram_write_gib": gib / 2, "dram_energy_j": 0.28 * t + 0.11 * gib})
        runs.loc[0, "case_id"] = "idle"
        runs.loc[1, "dram_read_gib"] = np.nan
        fit, x = common.dram_model(runs)
        self.assertAlmostEqual(fit["base_w"], 0.28)
        self.assertAlmostEqual(fit["j_per_gib"], 0.11)
        self.assertAlmostEqual(fit["r2"], 1.0)
        self.assertEqual(fit["n"], 38)
        np.testing.assert_allclose(x["dram_model_err_pct"], 0, atol=1e-9)

    def test_write_csv_and_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            common.write_csv(pd.DataFrame({"a": [1.23456789]}), "t", out=tmp)
            with open(os.path.join(tmp, "t.csv"), encoding="utf-8") as f:
                self.assertEqual(f.read().split(), ["a", "1.23457"])
            common.setup_style()
            fig, ax = common.plt.subplots(figsize=(common.HALF_W, 2))
            ax.plot([1, 2], [1, 2], color=common.JOB["cpu"])
            common.save_figure(fig, "f", figures=os.path.join(tmp, "fig"), preview=os.path.join(tmp, "pre"))
            self.assertTrue(os.path.getsize(os.path.join(tmp, "fig", "f.pdf")) > 0)
            self.assertTrue(os.path.getsize(os.path.join(tmp, "pre", "f.png")) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
