"""flow_context builder. All series are synthetic fixtures built in the tests."""

import math
import unittest
from datetime import date, timedelta

from app.ml import features_daily as fd
from app.ml.features_daily import AssetDay, ProtocolInputs
from app.ml.flow_context import FLOW_CONTEXT_DAYS, build_flow_context, context_from_features
from tests.test_features_daily import START, market

DAY = timedelta(days=1)


def to_rows(inputs: dict[str, ProtocolInputs]) -> tuple[list[dict], list[dict]]:
    protocols = [{"slug": s, "category": p.category} for s, p in inputs.items()]
    rows = []
    for s, p in inputs.items():
        rows += [{"slug": s, "metric": "tvl.usd.daily", "day": d, "symbol": None, "value": v} for d, v in p.tvl.items()]
        rows += [{"slug": s, "metric": "tvl.borrowed.usd.daily", "day": d, "symbol": None, "value": v} for d, v in p.borrowed.items()]
        for d, assets in p.tokens.items():
            for sym, a in assets.items():
                rows.append({"slug": s, "metric": "tokens.qty.daily", "day": d, "symbol": sym, "value": a.quantity})
                if a.value_usd is not None:
                    rows.append({"slug": s, "metric": "tokens.usd.daily", "day": d, "symbol": sym, "value": a.value_usd})
    return protocols, rows


def full_history_context(inputs, slug, day, **kw):
    f = next(x for x in fd.compute_daily_features(inputs, **kw)[slug] if x.day == day)
    return context_from_features(f, bool(inputs[slug].borrowed))


def last_day(inputs):
    return max(inputs["kamino"].tvl)


class AgreementTests(unittest.TestCase):
    def test_matches_compute_daily_features_over_full_history(self):
        inputs = market(days=260, flows={("jito", 259): -0.05})
        protocols, rows = to_rows(inputs)
        d = last_day(inputs)
        for slug in ("kamino", "jito", "marinade", "orca"):
            self.assertEqual(build_flow_context(protocols, rows, slug, d), full_history_context(inputs, slug, d), slug)

    def test_a_120_day_token_window_gives_the_same_latest_day(self):
        inputs = market(days=400)
        protocols, rows = to_rows(inputs)
        d = last_day(inputs)
        start = d - timedelta(days=FLOW_CONTEXT_DAYS - 1)
        windowed = [r for r in rows if r["metric"] == "tvl.usd.daily" or r["day"] >= start]
        for slug in ("kamino", "jito", "marinade"):
            full = build_flow_context(protocols, rows, slug, d)
            self.assertEqual(build_flow_context(protocols, windowed, slug, d), full, slug)
            self.assertIsNotNone(full["flow"]["robust_z"], slug)  # history sufficient for flow_z

    def test_rows_after_the_measurement_day_are_never_used(self):
        inputs = market(days=220, flows={("kamino", 219): -0.4})
        protocols, rows = to_rows(inputs)
        d = START + 200 * DAY
        earlier = [r for r in rows if r["day"] <= d]
        self.assertEqual(build_flow_context(protocols, rows, "kamino", d), build_flow_context(protocols, earlier, "kamino", d))

    def test_measurement_day_is_the_requested_baseline_day(self):
        inputs = market(days=200)
        protocols, rows = to_rows(inputs)
        d = START + 150 * DAY
        ctx = build_flow_context(protocols, rows, "jito", d)
        self.assertEqual((ctx["measurement_day"], ctx["previous_day"]), (d.isoformat(), (d - DAY).isoformat()))

    def test_lagging_token_history_is_reported_not_substituted(self):
        inputs = market(days=200)
        d = last_day(inputs)
        del inputs["jito"].tokens[d]
        ctx = build_flow_context(*to_rows(inputs), "jito", d)
        self.assertEqual(ctx["measurement_day"], d.isoformat())
        self.assertFalse(ctx["available"])
        self.assertIn({"field": "flow_log", "code": "no_observation"}, [{k: u[k] for k in ("field", "code")} for u in ctx["unavailable"]])


class UnavailableTests(unittest.TestCase):
    def test_no_token_data_is_honest_and_has_no_zeros(self):
        inputs = market(days=200)
        ctx = build_flow_context(*to_rows(inputs), "orca", last_day(inputs))
        self.assertFalse(ctx["available"])
        self.assertIsNone(ctx["flow"])
        self.assertIsNone(ctx["contributions"])
        self.assertIsNone(ctx["non_flow_effect"])
        self.assertIsNone(ctx["lending"])
        codes = {u["field"]: u["code"] for u in ctx["unavailable"]}
        self.assertEqual(codes["flow_log"], "no_token_data")
        self.assertEqual(codes["utilisation"], "not_applicable")

    def test_missing_previous_day(self):
        inputs = market(days=200)
        d = last_day(inputs)
        del inputs["marinade"].tokens[d - DAY]
        ctx = build_flow_context(*to_rows(inputs), "marinade", d)
        self.assertIsNone(ctx["flow"])
        self.assertEqual({u["field"]: u["code"] for u in ctx["unavailable"]}["flow_log"], "previous_day_missing")

    def test_below_min_base(self):
        d0, d1 = date(2026, 1, 1), date(2026, 1, 2)
        small = ProtocolInputs("x", "DEX", tvl={d0: 5e6, d1: 4e6},
                               tokens={d0: {"USDC": AssetDay(5e6, 5e6)}, d1: {"USDC": AssetDay(4e6, 4e6)}})
        ctx = build_flow_context(*to_rows({"x": small}), "x", d1)
        self.assertIsNone(ctx["flow"])
        self.assertEqual({u["field"]: u["code"] for u in ctx["unavailable"]}["flow_log"], "below_min_base")

    def test_low_coverage_reports_shares_but_no_flow(self):
        d0, d1 = date(2026, 1, 1), date(2026, 1, 2)
        p = ProtocolInputs("x", "DEX", tvl={d0: 1e8, d1: 1e8},
                           tokens={d0: {"A": AssetDay(80, 8e7), "B": AssetDay(20, 2e7)}, d1: {"A": AssetDay(80, 8e7), "NEW": AssetDay(1, 1e6)}})
        ctx = build_flow_context(*to_rows({"x": p}), "x", d1)
        self.assertIsNone(ctx["flow"])
        self.assertIsNone(ctx["contributions"])
        self.assertEqual(ctx["coverage"]["matched_share"], 0.8)
        self.assertAlmostEqual(ctx["coverage"]["removed_share"], 0.2)
        self.assertAlmostEqual(ctx["coverage"]["added_share"], 0.01)
        self.assertEqual({u["field"]: u["code"] for u in ctx["unavailable"]}["flow_log"], "asset_set_changed")

    def test_zero_dispersion_is_not_a_zero_z(self):
        inputs = market(days=200)
        jito = inputs["jito"]
        # Constant quantity with power-of-two prices, so price * quantity is exact in floating point and
        # every daily flow is exactly 0. (With arbitrary prices the feature module sees ~1e-16 rounding
        # noise instead of zero dispersion; see the Phase 3B.3A report.)
        for i, d in enumerate(sorted(jito.tokens)):
            jito.tokens[d] = {"SOL": AssetDay(float(2 ** 24), float(2 ** 24 * 2 ** (i % 3)))}
        ctx = build_flow_context(*to_rows(inputs), "jito", last_day(inputs))
        self.assertEqual(ctx["flow"]["log_change"], 0.0)
        self.assertIsNone(ctx["flow"]["robust_z"])
        self.assertEqual({u["field"]: u["code"] for u in ctx["unavailable"]}["flow_z"], "zero_dispersion")

    def test_no_measurement_day(self):
        ctx = build_flow_context([], [], "x", None)
        self.assertEqual((ctx["measurement_day"], ctx["available"]), (None, False))


class DriftRegimeTests(unittest.TestCase):
    def test_residual_regime_returns_regime_and_no_invented_flow(self):
        inputs = market(days=200)
        brk = START + 150 * DAY
        drift = inputs["drift"]
        drift.tokens = {d: {"USDC": AssetDay(v * (0.02 if d >= brk else 1), v * (0.02 if d >= brk else 1))} for d, v in drift.tvl.items()}
        for d in list(drift.tvl):
            if d >= brk:
                drift.tvl[d] *= 0.02
        protocols, rows = to_rows(inputs)
        later = build_flow_context(protocols, rows, "drift", brk + 20 * DAY, regime_breaks={"drift": brk})
        self.assertEqual(later["regime"], "residual")
        self.assertIsNone(later["flow"])
        codes = {u["field"]: u["code"] for u in later["unavailable"]}
        self.assertEqual(codes["tvl_log_return"], "regime_break")
        self.assertEqual(codes["flow_log"], "below_min_base")
        on_break = build_flow_context(protocols, rows, "drift", brk, regime_breaks={"drift": brk})
        self.assertEqual(on_break["regime"], "residual")
        self.assertLess(on_break["flow"]["log_change"], -3)  # the genuine outflow itself stays visible
        self.assertIsNone(on_break["non_flow_effect"])  # no TVL comparison across the break


class ContributionTests(unittest.TestCase):
    D0, D1 = date(2026, 1, 1), date(2026, 1, 2)

    def ctx(self, y, t):
        p = ProtocolInputs("x", "DEX", tvl={self.D0: sum(a.value_usd for a in y.values()), self.D1: sum(a.value_usd for a in t.values())},
                           tokens={self.D0: y, self.D1: t})
        return build_flow_context(*to_rows({"x": p}), "x", self.D1)

    def test_values_are_shares_of_previous_day_matched_value(self):
        y = {"SOL": AssetDay(1e5, 1e7), "USDC": AssetDay(4e7, 4e7)}
        t = {"SOL": AssetDay(0.5e5, 0.4e7), "USDC": AssetDay(4.5e7, 4.5e7)}  # SOL also fell in price
        c = self.ctx(y, t)["contributions"]
        self.assertEqual(c["negative"], [{"symbol": "SOL", "share": round(-0.5e7 / 5e7, 6)}])
        self.assertEqual(c["positive"], [{"symbol": "USDC", "share": round(0.5e7 / 5e7, 6)}])
        self.assertAlmostEqual(c["flow_total"], 0.0)  # quantities at yesterday's prices: -$5M + $5M
        self.assertEqual(c["other_assets"], 0.0)

    def test_top_ten_truncation_keeps_the_remainder(self):
        y = {f"T{i:02d}": AssetDay(1e6, 1e6 * (i + 1)) for i in range(15)}
        t = {s: AssetDay(a.quantity * (1.1 if i % 2 else 0.95), 0.0 + a.value_usd) for i, (s, a) in enumerate(y.items())}
        ctx = self.ctx(y, t)
        c = ctx["contributions"]
        listed = c["positive"] + c["negative"]
        self.assertEqual(len(listed), 10)
        base = sum(a.value_usd for a in y.values())
        true_total = sum(a.value_usd * (t[s].quantity / a.quantity - 1) for s, a in y.items()) / base
        self.assertAlmostEqual(c["flow_total"], true_total, places=6)
        self.assertAlmostEqual(c["flow_total"], math.expm1(ctx["flow"]["log_change"]), places=6)
        self.assertAlmostEqual(c["other_assets"], c["flow_total"] - c["listed_total"], places=6)
        self.assertNotAlmostEqual(c["other_assets"], 0.0, places=4)

    def test_quantities_are_valued_never_summed(self):
        y = {"BONK": AssetDay(1e12, 1e7), "USDC": AssetDay(4e7, 4e7)}
        t = {"BONK": AssetDay(2e12, 2e7), "USDC": AssetDay(4e7, 4e7)}  # a trillion more BONK = +$10M, not +1e12
        ctx = self.ctx(y, t)
        self.assertAlmostEqual(ctx["contributions"]["flow_total"], 1e7 / 5e7, places=6)
        self.assertAlmostEqual(ctx["flow"]["pct_change"], 0.2, places=6)


class LendingTests(unittest.TestCase):
    def test_lending_only_with_borrowed_history(self):
        inputs = market(days=200)
        protocols, rows = to_rows(inputs)
        d = last_day(inputs)
        kamino = build_flow_context(protocols, rows, "kamino", d)
        self.assertAlmostEqual(kamino["lending"]["utilisation"], round(0.66 / 1.66, 6))
        self.assertIn("available liquidity", kamino["lending"]["definition"])
        self.assertIsNone(build_flow_context(protocols, rows, "jito", d)["lending"])

    def test_zero_or_missing_denominators_never_divide(self):
        inputs = market(days=150)
        k = inputs["kamino"]
        d = last_day(inputs)
        k.borrowed[d] = 0.0
        ctx = build_flow_context(*to_rows(inputs), "kamino", d)
        self.assertIsNone(ctx["lending"]["utilisation"])
        self.assertIsNone(ctx["lending"]["utilisation_change"])
        del k.borrowed[d]
        ctx = build_flow_context(*to_rows(inputs), "kamino", d)
        self.assertIsNone(ctx["lending"]["borrowed_usd"])
        self.assertEqual({u["field"]: u["code"] for u in ctx["unavailable"]}["utilisation"], "no_observation")


class WordingTests(unittest.TestCase):
    def test_disclaimer_never_claims_deposits_or_pure_price(self):
        inputs = market(days=150)
        ctx = build_flow_context(*to_rows(inputs), "kamino", last_day(inputs))
        self.assertIn("not direct evidence of user deposits or withdrawals", ctx["disclaimer"])
        self.assertIn("not a pure price impact", ctx["disclaimer"])
        self.assertEqual(ctx["source"]["adapters"], ["defillama_tvl_history@1", "defillama_token_history@1"])
        self.assertEqual(ctx["feature_version"], "features-daily-v1")


if __name__ == "__main__":
    unittest.main()
