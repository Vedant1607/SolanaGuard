"""tvl-robust-v1 model tests. All series here are synthetic and exist only in tests."""

import math
import unittest
from datetime import date, timedelta

import numpy as np

from app.ml import tvl_robust as m
from app.ml.tvl_robust import DailyObservation, classify, compute_scores, explain

START = date(2025, 1, 1)


def synthetic_market(protocols=5, days=300, seed=7, shocks=None, skip=None, override=None):
    """Random-walk TVL per protocol: a shared market factor plus idiosyncratic noise.

    ``shocks``: {(slug, day_index): log_shock}. ``skip``: {slug: {day_index}} left out.
    ``override``: {(slug, day_index): raw value} (e.g. 0 or NaN).
    """
    rng = np.random.default_rng(seed)
    market = rng.normal(0, 0.02, days)
    series = {}
    for p in range(protocols):
        slug = f"p{p}"
        log_tvl = math.log(1e8 * (p + 1))
        rows = []
        for t in range(days):
            log_tvl += market[t] + rng.normal(0, 0.01) + (shocks or {}).get((slug, t), 0.0)
            if t in (skip or {}).get(slug, set()):
                continue
            value = (override or {}).get((slug, t), math.exp(log_tvl))
            rows.append(DailyObservation(START + timedelta(days=t), value, f"{slug}-{t}"))
        series[slug] = rows
    return series


def by_day(scores, slug):
    return {s.day: s for s in scores[slug]}


class LookAheadTests(unittest.TestCase):
    def test_appending_future_days_never_changes_past_scores(self):
        full = synthetic_market(days=320, shocks={("p0", 300): -0.4, ("p2", 310): 0.3})
        cut = {slug: [o for o in rows if o.day < START + timedelta(days=250)] for slug, rows in full.items()}
        past, later = compute_scores(cut), compute_scores(full)
        for slug in full:
            a, b = by_day(past, slug), by_day(later, slug)
            self.assertEqual(sorted(a), sorted(d for d in b if d < START + timedelta(days=250)))
            for day, s in a.items():
                t = b[day]
                for attr in ("robust_z", "market_z", "drawdown", "excess_return", "peer_return", "baseline_points", "history_days"):
                    self.assertEqual(getattr(s, attr), getattr(t, attr), (slug, day, attr))

    def test_the_scored_day_is_not_in_its_own_baseline(self):
        series = synthetic_market(days=200, shocks={("p1", 150): -0.5})
        shocked = by_day(compute_scores(series), "p1")[START + timedelta(days=150)]
        self.assertEqual(shocked.baseline_points, 90)  # days 60..149, not day 150
        self.assertLess(shocked.robust_z, -10)  # a baseline containing the shock would dampen it


class InsufficientHistoryTests(unittest.TestCase):
    def test_fewer_than_90_prior_observations_are_not_scored(self):
        scores = compute_scores(synthetic_market(days=90))
        self.assertTrue(all(not s.scored and "insufficient_history" in s.reasons for s in scores["p0"]))
        self.assertEqual(classify(scores["p0"][-1], scores["p0"][-1].day)[0], "INSUFFICIENT_HISTORY")

    def test_day_91_is_the_first_scoreable_day(self):
        scores = by_day(compute_scores(synthetic_market(days=100)), "p0")
        self.assertFalse(scores[START + timedelta(days=89)].scored)
        self.assertTrue(scores[START + timedelta(days=90)].scored)

    def test_no_observations(self):
        self.assertEqual(classify(None, START), ("INSUFFICIENT_HISTORY", ["no_observations"]))

    def test_stale_latest_day_is_not_reported_as_current(self):
        scores = compute_scores(synthetic_market(days=150))
        latest = scores["p0"][-1]
        self.assertIn(classify(latest, latest.day)[0], ("NORMAL", "UNUSUAL"))
        label, reasons = classify(latest, latest.day + timedelta(days=4))
        self.assertEqual((label, reasons), ("INSUFFICIENT_HISTORY", ["stale_history"]))


class MissingAndInvalidDataTests(unittest.TestCase):
    def test_gaps_are_not_filled_and_break_the_next_return(self):
        series = synthetic_market(days=200, skip={"p0": {150, 151}})
        days = by_day(compute_scores(series), "p0")
        self.assertNotIn(START + timedelta(days=150), days)
        after_gap = days[START + timedelta(days=152)]
        self.assertIsNone(after_gap.log_return)
        self.assertIn("previous_day_missing", after_gap.reasons)
        self.assertFalse(after_gap.scored)
        self.assertEqual(len(days), 198)

    def test_zero_nan_and_negative_tvl_are_excluded(self):
        override = {("p0", 160): 0.0, ("p0", 170): float("nan"), ("p0", 180): -1.0}
        days = by_day(compute_scores(synthetic_market(days=200, override=override)), "p0")
        for t in (160, 170, 180):
            self.assertNotIn(START + timedelta(days=t), days)
            self.assertIn("previous_day_missing", days[START + timedelta(days=t + 1)].reasons)
        self.assertTrue(all(math.isfinite(s.robust_z) for s in days.values() if s.scored))

    def test_too_few_peers_leaves_excess_undefined(self):
        scores = compute_scores(synthetic_market(protocols=3, days=150))  # 2 peers each
        self.assertTrue(all("insufficient_peers" in s.reasons for s in scores["p0"][1:]))


class ZeroDispersionTests(unittest.TestCase):
    def test_identical_protocols_have_no_dispersion_and_are_not_scored(self):
        base = synthetic_market(protocols=1, days=150)["p0"]
        series = {f"p{i}": [DailyObservation(o.day, o.value, f"p{i}-{o.day}") for o in base] for i in range(4)}
        late = compute_scores(series)["p0"][-1]
        self.assertIsNone(late.robust_z)
        self.assertIn("zero_dispersion", late.reasons)
        self.assertEqual(classify(late, late.day)[0], "INSUFFICIENT_HISTORY")

    def test_zero_mad_falls_back_to_mean_absolute_deviation(self):
        base = synthetic_market(protocols=1, days=150)["p0"]
        series = {f"p{i}": [DailyObservation(o.day, o.value, f"p{i}-{o.day}") for o in base] for i in range(4)}
        # p0 deviates from its identical peers on a few baseline days only -> MAD 0, mean AD > 0.
        bumped = {START + timedelta(days=t) for t in (100, 110, 120)}
        factor, rows = 1.0, []
        for o in series["p0"]:
            if o.day in bumped:
                factor *= 1.05
            rows.append(DailyObservation(o.day, o.value * factor, o.observation_id))
        series["p0"] = rows
        late = compute_scores(series)["p0"][-1]
        self.assertEqual(late.scale_method, "mean_absolute_deviation")
        self.assertTrue(late.scored and math.isfinite(late.robust_z))


class MarketVersusProtocolTests(unittest.TestCase):
    def test_market_wide_drop_is_not_a_protocol_specific_anomaly(self):
        shocks = {(f"p{p}", 200): -0.35 for p in range(5)}
        day = by_day(compute_scores(synthetic_market(days=210, shocks=shocks)), "p0")[START + timedelta(days=200)]
        self.assertLess(day.log_return, -0.3)
        self.assertLess(abs(day.robust_z), m.THRESHOLD)  # excess is small
        self.assertGreaterEqual(abs(day.market_z), m.THRESHOLD)  # the market moved
        self.assertEqual(classify(day, day.day)[0], "NORMAL")
        self.assertTrue(any("market-wide" in line for line in explain(day)))

    def test_protocol_specific_drop_is_unusual(self):
        day = by_day(compute_scores(synthetic_market(days=210, shocks={("p0", 200): -0.35})), "p0")[START + timedelta(days=200)]
        self.assertLessEqual(day.robust_z, -m.THRESHOLD)
        self.assertLess(abs(day.market_z), m.THRESHOLD)
        self.assertEqual(classify(day, day.day)[0], "UNUSUAL")
        text = " ".join(explain(day))
        self.assertIn("protocol-specific", text)
        self.assertIn("decline", text)

    def test_peer_median_leaves_the_protocol_out(self):
        scores = compute_scores(synthetic_market(protocols=4, days=210, shocks={("p0", 200): -0.35}))
        p0 = by_day(scores, "p0")[START + timedelta(days=200)]
        p1 = by_day(scores, "p1")[START + timedelta(days=200)]
        self.assertEqual(p0.peers, 3)
        self.assertGreater(p0.peer_return, -0.1)  # p0's own crash is not in its reference
        self.assertLess(abs(p1.robust_z), m.THRESHOLD)  # one peer's crash does not move the others' median much

    def test_drawdown_uses_the_preceding_30_day_high(self):
        day = by_day(compute_scores(synthetic_market(days=210, shocks={("p0", 200): -0.35})), "p0")[START + timedelta(days=200)]
        self.assertLess(day.drawdown, -0.25)
        self.assertEqual(day.drawdown_points, 30)


class InvariantTests(unittest.TestCase):
    def test_constants_match_the_specification(self):
        self.assertEqual((m.MODEL_VERSION, m.BASELINE_DAYS, m.MIN_HISTORY_DAYS, m.DRAWDOWN_DAYS), ("tvl-robust-v1", 90, 90, 30))
        self.assertIn("not a probability", m.DISCLAIMER)


if __name__ == "__main__":
    unittest.main()
