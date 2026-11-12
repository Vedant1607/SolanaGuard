"""multivariate-eval-v1: leakage guards and decision rules. All rows are synthetic fixtures."""

import importlib.util
import math
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import numpy as np

from app.ml.features_daily import DailyFeatures

_spec = importlib.util.spec_from_file_location("evaluate_multivariate", Path(__file__).resolve().parents[1] / "scripts" / "evaluate_multivariate.py")
ev = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ev  # dataclasses resolve their module while it executes
_spec.loader.exec_module(ev)

START = date(2023, 1, 1)


def row(slug, day, category="DEX", flow=False, borrow=False, baseline_z=None, **features):
    f = DailyFeatures(slug=slug, day=day)
    for k, v in features.items():
        setattr(f, k, v)
    return ev.Row(slug, day, category, f, baseline_z, None, None, flow, borrow)


def history(days=900, seed=5, slugs=("a", "b", "c")):
    rng = np.random.default_rng(seed)
    rows = []
    for t in range(days):
        day = START + timedelta(days=t)
        for s in slugs:
            e = float(rng.normal(0, 0.01))
            rows.append(row(s, day, baseline_z=float(rng.normal(0, 2)), excess_return=e, excess_return_z=e / 0.01,
                            drawdown_30d=-abs(float(rng.normal(0, 0.05))), vol_ratio_7_90=abs(float(rng.normal(1, 0.2)))))
    return rows


class SplitTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(ev.split_of(date(2024, 6, 30)), "train")
        self.assertEqual(ev.split_of(date(2024, 7, 1)), "calibration")
        self.assertEqual(ev.split_of(date(2024, 12, 31)), "calibration")
        self.assertEqual(ev.split_of(date(2025, 1, 1)), "holdout")


class TransparentTests(unittest.TestCase):
    def test_gates_and_threshold_ignore_holdout(self):
        rows = history()
        model = ev.fit_frozen(ev.TransparentMaxZ, rows, 0.01)
        poisoned = rows + [row("a", date(2025, 3, 1) + timedelta(days=i), excess_return=5.0, excess_return_z=500.0) for i in range(500)]
        again = ev.fit_frozen(ev.TransparentMaxZ, poisoned, 0.01)
        self.assertEqual((model.gates, model.threshold), (again.gates, again.threshold))

    def test_tiny_but_extreme_change_is_below_the_gate(self):
        model = ev.fit_frozen(ev.TransparentMaxZ, history(), 0.01)
        tiny = row("a", date(2025, 2, 1), excess_return=0.0001, excess_return_z=50.0)
        big = row("a", date(2025, 2, 1), excess_return=0.2, excess_return_z=50.0)
        r_tiny, r_big = model.score(tiny), model.score(big)
        self.assertTrue(r_tiny.scored)
        self.assertFalse(r_tiny.flagged)
        self.assertEqual(r_tiny.score, 0.0)
        self.assertTrue(r_big.flagged)
        self.assertEqual(r_big.explanation[0]["signal"], "excess")

    def test_liquid_staking_uses_flow_not_both(self):
        lst = row("j", date(2025, 2, 1), category="LIQUID_STAKING", flow=True, excess_return=-0.2, excess_return_z=-30.0,
                  flow_log=-0.19, flow_z=-28.0)
        self.assertEqual([s[0] for s in ev.signals(lst)], ["flow"])
        lst_no_flow = row("j", date(2025, 2, 1), category="LIQUID_STAKING", excess_return=-0.2, excess_return_z=-30.0)
        self.assertEqual([s[0] for s in ev.signals(lst_no_flow)], ["excess"])
        lend = row("k", date(2025, 2, 1), category="LENDING", flow=True, borrow=True, excess_return=-0.2, excess_return_z=-30.0,
                   flow_log=-0.1, flow_z=-9.0, utilisation_change=0.06, utilisation_change_z=17.0)
        self.assertEqual([s[0] for s in ev.signals(lend)], ["excess", "flow", "utilisation"])

    def test_calibrated_rate_does_not_exceed_target(self):
        model = ev.fit_frozen(ev.TransparentMaxZ, history(), 0.02)
        self.assertLessEqual(model.calibration_rate, 0.02)


class IsolationForestTests(unittest.TestCase):
    def test_missing_features_are_unscored_never_imputed(self):
        model = ev.fit_frozen(lambda: ev.IFComposite("C1_iforest_core"), history(), 0.01)
        incomplete = row("a", date(2025, 2, 1), excess_return_z=3.0, drawdown_30d=None, vol_ratio_7_90=1.0)
        res = model.score_rows([incomplete])[incomplete.key]
        self.assertFalse(res.scored)
        self.assertIn("incomplete_features:drawdown_30d", res.reason)

    def test_deterministic_for_a_seed(self):
        rows = history()
        a = ev.score_all(ev.fit_frozen(lambda: ev.IFComposite("C1_iforest_core", 11), rows, 0.01), rows)
        b = ev.score_all(ev.fit_frozen(lambda: ev.IFComposite("C1_iforest_core", 11), rows, 0.01), rows)
        self.assertEqual({k: v.score for k, v in a.items()}, {k: v.score for k, v in b.items()})

    def test_insufficient_training_is_reported_not_relaxed(self):
        rows = [r for r in history(days=900) if r.slug == "a"]
        k = [row("k", r.day, category="LENDING", flow=True, borrow=True, excess_return_z=0.1, drawdown_30d=-0.01, vol_ratio_7_90=1.0,
                 flow_z=0.1, utilisation_change_z=0.1) for r in rows if r.day >= date(2024, 4, 1)]  # ~90 training days
        model = ev.fit_frozen(lambda: ev.IFComposite("C3_iforest_tvl_flow_util"), rows + k, 0.01)
        self.assertEqual(model.groups["flow_util"].status, "insufficient_training")
        res = model.score_rows([k[-1]])[k[-1].key]
        self.assertFalse(res.scored)
        self.assertEqual(res.reason, "flow_util:insufficient_training")

    def test_protocols_use_their_capable_schema(self):
        m = ev.IFComposite("C3_iforest_tvl_flow_util")
        self.assertEqual(m.assign(row("r", START)), "core")
        self.assertEqual(m.assign(row("j", START, category="LIQUID_STAKING", flow=True)), "flow_lst")
        self.assertEqual(m.assign(row("d", START, category="PERPS", flow=True)), "flow")
        self.assertEqual(m.assign(row("k", START, category="LENDING", flow=True, borrow=True)), "flow_util")


class WalkForwardTests(unittest.TestCase):
    def test_each_refit_sees_only_rows_before_its_month(self):
        seen = []

        class Spy(ev.TransparentMaxZ):
            def fit(self, train, calib, target_rate):
                seen.append((max(r.day for r in train + calib), self))
                return super().fit(train, calib, target_rate)

        rows = history(days=800)
        results, _ = ev.walk_forward(Spy, rows, 0.01)
        months = ev.month_starts(rows)
        self.assertEqual(len(seen), len(months))
        for (latest, _), start in zip(seen, months):
            self.assertLess(latest, start)
        self.assertTrue(all(ev.split_of(k[1]) == "holdout" for k in results))


class ConsistencyTests(unittest.TestCase):
    def test_rules(self):
        flagged, quiet, unscored = ev.Result(True, True, 9.0), ev.Result(True, False, 1.0), ev.Result(False, reason="x")
        self.assertEqual(ev.consistency("flag", flagged, "quantity"), "consistent")
        self.assertEqual(ev.consistency("flag", quiet, "quantity"), "missed")
        self.assertEqual(ev.consistency("flag", unscored, "quantity"), "not_scored")
        self.assertEqual(ev.consistency("no_flag", flagged, "no_flow_data"), "inconsistent")
        self.assertEqual(ev.consistency("no_flag", unscored, "no_flow_data"), "consistent")
        self.assertEqual(ev.consistency("flag_only_if_quantity", flagged, "price_or_other"), "inconsistent")
        self.assertEqual(ev.consistency("flag_only_if_quantity", flagged, "quantity"), "consistent")
        self.assertEqual(ev.consistency("flag_only_if_quantity", quiet, "price_or_other"), "consistent")
        self.assertEqual(ev.consistency("undetermined", flagged, "no_flow_data"), "n/a")

    def test_transforms_are_parameter_free(self):
        self.assertAlmostEqual(ev.transform("flow_z", -10.0), -math.log1p(10.0))
        self.assertAlmostEqual(ev.transform("vol_ratio_7_90", 1.0), math.log1p(1.0))
        self.assertEqual(ev.transform("drawdown_30d", -0.2), -0.2)
        self.assertIsNone(ev.vector(row("a", START, excess_return_z=float("nan"), drawdown_30d=0.0, vol_ratio_7_90=1.0), ev.GROUP_FEATURES["core"]))


if __name__ == "__main__":
    unittest.main()
