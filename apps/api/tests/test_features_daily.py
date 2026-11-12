"""features-daily-v1. Every series here is a synthetic fixture built inside the tests."""

import math
import unittest
from datetime import date, timedelta

import numpy as np

from app.ml import features_daily as fd
from app.ml.features_daily import AssetDay, ProtocolInputs, compute_daily_features, fixed_price_flow, inputs_from_rows

START = date(2025, 1, 1)
DAY = timedelta(days=1)


def market(days=200, seed=3, shocks=None, flows=None, skip=None, sol_shocks=None):
    """Six protocols with a shared SOL factor. Liquid-staking TVL = SOL qty x SOL price.

    shocks: {(slug, t): log shock to USD TVL}; flows: {(slug, t): log shock to quantities};
    skip: {slug: {t}} days missing at source; sol_shocks: {t: log shock to SOL price}.
    """
    rng = np.random.default_rng(seed)
    sol = math.log(100.0)
    level = {s: math.log(v) for s, v in {"raydium": 9e8, "orca": 3e8, "kamino": 1.2e9, "drift": 3e8}.items()}
    qty = {"jito": math.log(1.3e7), "marinade": math.log(3e6)}
    out = {s: ProtocolInputs(s, c) for s, c in {"raydium": "DEX", "orca": "DEX", "kamino": "LENDING", "drift": "PERPS",
                                                 "jito": "LIQUID_STAKING", "marinade": "LIQUID_STAKING"}.items()}
    for t in range(days):
        day = START + t * DAY
        sol_r = rng.normal(0, 0.03) + (sol_shocks or {}).get(t, 0.0)
        sol += sol_r
        for s in level:
            level[s] += 0.6 * sol_r + rng.normal(0, 0.01) + (shocks or {}).get((s, t), 0.0)
        for s in qty:
            qty[s] += rng.normal(0, 0.004) + (flows or {}).get((s, t), 0.0)
        for s, p in out.items():
            if t in (skip or {}).get(s, set()):
                continue
            if s in qty:
                q, price = math.exp(qty[s]), math.exp(sol)
                p.tvl[day] = q * price
                p.tokens[day] = {"SOL": AssetDay(q, q * price)}
            else:
                p.tvl[day] = math.exp(level[s])
    # Kamino: two assets and a borrowed series (utilisation ~0.4).
    k = out["kamino"]
    for day, tvl in k.tvl.items():
        k.tokens[day] = {"USDC": AssetDay(tvl * 0.6, tvl * 0.6), "SOL": AssetDay(tvl * 0.4 / 100, tvl * 0.4)}
        k.borrowed[day] = tvl * 0.66
    return out


def by_day(results, slug):
    return {f.day: f for f in results[slug]}


class NoLookaheadTests(unittest.TestCase):
    FIELDS = ("tvl_log_return", "reference_return", "excess_return", "excess_return_z", "drawdown_30d", "vol_ratio_7_90",
              "sol_log_return", "flow_log", "flow_z", "flow_coverage", "non_flow_effect", "utilisation", "utilisation_change",
              "utilisation_change_z", "borrowed_log_change", "peers", "regime", "flow_contributions", "unavailable")

    def test_appending_future_days_never_changes_earlier_features(self):
        full = market(days=260, shocks={("orca", 240): -0.5}, flows={("jito", 250): -0.3})
        cut_day = START + 200 * DAY
        cut = {s: ProtocolInputs(s, p.category, {d: v for d, v in p.tvl.items() if d < cut_day},
                                 {d: v for d, v in p.tokens.items() if d < cut_day},
                                 {d: v for d, v in p.borrowed.items() if d < cut_day}) for s, p in full.items()}
        early, late = compute_daily_features(cut), compute_daily_features(full)
        for slug in full:
            a, b = by_day(early, slug), by_day(late, slug)
            self.assertTrue(a)
            for day, f in a.items():
                for name in self.FIELDS:
                    self.assertEqual(getattr(f, name), getattr(b[day], name), (slug, day, name))

    def test_deterministic(self):
        a, b = compute_daily_features(market()), compute_daily_features(market())
        self.assertEqual([vars(f) for f in a["kamino"]], [vars(f) for f in b["kamino"]])

    def test_scored_day_is_not_in_its_own_baseline(self):
        res = by_day(compute_daily_features(market(shocks={("raydium", 150): -0.6})), "raydium")
        self.assertLess(res[START + 150 * DAY].excess_return_z, -10)


class InsufficientAndMissingTests(unittest.TestCase):
    def test_short_history_is_explicitly_unavailable(self):
        res = by_day(compute_daily_features(market(days=40)), "orca")
        last = res[START + 39 * DAY]
        self.assertIsNone(last.excess_return_z)
        self.assertEqual(last.unavailable["excess_return_z"], "insufficient_baseline")
        self.assertEqual(last.unavailable["vol_ratio_7_90"], "insufficient_baseline")
        self.assertIsNone(res[START].tvl_log_return)
        self.assertEqual(res[START].unavailable["tvl_log_return"], "previous_day_missing")

    def test_gaps_are_never_filled(self):
        res = by_day(compute_daily_features(market(skip={"jito": {100, 101}})), "jito")
        self.assertNotIn(START + 100 * DAY, res)
        after = res[START + 102 * DAY]
        self.assertIsNone(after.tvl_log_return)
        self.assertIsNone(after.flow_log)
        self.assertEqual(after.unavailable["flow_log"], "previous_day_missing")

    def test_protocol_without_tokens_or_borrowing(self):
        f = compute_daily_features(market())["orca"][-1]
        self.assertEqual(f.unavailable["flow_log"], "no_token_data")
        self.assertEqual(f.unavailable["utilisation"], "not_applicable")
        self.assertIsNone(f.flow_log)


class FlowTests(unittest.TestCase):
    def test_price_moves_cancel_and_quantity_moves_count(self):
        y = {"SOL": AssetDay(1000, 100_000.0), "USDC": AssetDay(50_000, 50_000.0)}
        price_only = fixed_price_flow({"SOL": AssetDay(1000, 70_000.0), "USDC": AssetDay(50_000, 50_000.0)}, y)
        self.assertAlmostEqual(price_only["flow_log"], 0.0)
        outflow = fixed_price_flow({"SOL": AssetDay(500, 35_000.0), "USDC": AssetDay(50_000, 50_000.0)}, y)
        self.assertAlmostEqual(outflow["flow_log"], math.log(100_000 / 150_000))  # 500 SOL at yesterday's $100
        self.assertEqual(outflow["contributions"][0][0], "SOL")
        self.assertAlmostEqual(outflow["contributions"][0][1], -50_000 / 150_000)

    def test_quantities_of_different_assets_are_never_summed(self):
        y = {"BONK": AssetDay(1e9, 10.0), "USDC": AssetDay(1000, 1000.0)}
        t = {"BONK": AssetDay(2e9, 20.0), "USDC": AssetDay(1000, 1000.0)}  # a billion more BONK is worth $10, not 1e9
        self.assertAlmostEqual(fixed_price_flow(t, y)["flow_log"], math.log(1010 / 1010 * (1010 + 10) / 1010))

    def test_asset_additions_removals_and_unpriced_assets_are_reported(self):
        y = {"A": AssetDay(10, 900.0), "B": AssetDay(1, 100.0), "U": AssetDay(5, None)}
        t = {"A": AssetDay(10, 900.0), "NEW": AssetDay(3, 300.0), "U": AssetDay(5, None)}
        flow = fixed_price_flow(t, y)
        self.assertAlmostEqual(flow["coverage"], 0.9)
        self.assertAlmostEqual(flow["removed_share"], 0.1)
        self.assertAlmostEqual(flow["added_share"], 0.3)
        self.assertEqual(flow["unpriced"], 1)
        self.assertAlmostEqual(flow["flow_log"], 0.0)  # B and NEW are not counted as flows

    def test_low_coverage_makes_the_flow_unavailable(self):
        data = market(days=150)
        k = data["kamino"]
        d = START + 120 * DAY
        k.tokens[d] = {"USDC": k.tokens[d]["USDC"]}  # SOL (40% of value) vanished from the source
        f = by_day(compute_daily_features(data), "kamino")[d]
        self.assertIsNone(f.flow_log)
        self.assertEqual(f.unavailable["flow_log"], "asset_set_changed")
        self.assertAlmostEqual(f.flow_removed_share, 0.4, places=6)

    def test_market_wide_sol_crash_is_price_not_flow_for_liquid_staking(self):
        res = by_day(compute_daily_features(market(days=200, sol_shocks={150: -0.22})), "jito")
        crash = res[START + 150 * DAY]
        self.assertLess(crash.tvl_log_return, -0.15)
        self.assertLess(abs(crash.flow_z), 5)  # quantities did not move
        self.assertLess(abs(crash.excess_return_z), 5)  # reference is the SOL price itself
        self.assertEqual(crash.reference, "sol_price")
        self.assertLess(crash.non_flow_effect, -0.15)  # the move is explained by price, not flows

    def test_real_withdrawal_shows_as_a_flow(self):
        res = by_day(compute_daily_features(market(days=200, flows={("marinade", 150): -0.2})), "marinade")
        self.assertLess(res[START + 150 * DAY].flow_z, -10)


class PeerTests(unittest.TestCase):
    def test_category_aware_references(self):
        res = compute_daily_features(market())
        dex = by_day(res, "raydium")[START + 120 * DAY]
        self.assertEqual((dex.reference, dex.peers), ("peer_median", 3))  # orca, kamino, drift; never the LSTs
        lst = by_day(res, "jito")[START + 120 * DAY]
        self.assertEqual(lst.reference, "sol_price")
        self.assertAlmostEqual(lst.reference_return, lst.sol_log_return)

    def test_insufficient_peers(self):
        data = market(skip={"orca": {120}, "kamino": {120}})
        f = by_day(compute_daily_features(data), "raydium")[START + 120 * DAY]
        self.assertEqual(f.peers, 1)
        self.assertEqual(f.unavailable["excess_return"], "insufficient_peers")


class DriftRegimeTests(unittest.TestCase):
    def setUp(self):
        self.data = market(days=200)
        self.brk = START + 150 * DAY
        drift = self.data["drift"]
        for day in list(drift.tvl):
            if day >= self.brk:
                drift.tvl[day] = drift.tvl[day] * 0.02  # a residual balance, $6M-ish
        self.regimes = {"drift": self.brk}

    def test_post_cutoff_days_are_residual_and_excluded_from_peers(self):
        res = compute_daily_features(self.data, regime_breaks=self.regimes)
        d = by_day(res, "drift")
        self.assertEqual(d[self.brk].regime, "residual")
        self.assertEqual(d[self.brk - DAY].regime, "normal")
        for day in (self.brk, self.brk + 10 * DAY):
            self.assertIsNone(d[day].excess_return)
            self.assertEqual(d[day].unavailable["excess_return"], "regime_break")
        before = by_day(res, "raydium")[self.brk - DAY]
        after = by_day(res, "raydium")[self.brk + 5 * DAY]
        self.assertEqual((before.peers, after.peers), (3, 2))

    def test_without_the_cutoff_the_collapse_would_pollute_peers(self):
        cut = by_day(compute_daily_features(self.data, regime_breaks=self.regimes), "orca")[self.brk]
        raw = by_day(compute_daily_features(self.data, regime_breaks={}), "orca")[self.brk]
        self.assertNotEqual(cut.reference_return, raw.reference_return)

    def test_minimum_base_blocks_percentages_on_residual_balances(self):
        res = by_day(compute_daily_features(self.data, regime_breaks={}), "drift")
        later = res[self.brk + 5 * DAY]  # ~$6M base, below MIN_BASE_USD
        self.assertIsNone(later.tvl_log_return)
        self.assertEqual(later.unavailable["tvl_log_return"], "below_min_base")


class UtilisationTests(unittest.TestCase):
    def test_utilisation_uses_supplied_minus_borrowed_semantics(self):
        f = by_day(compute_daily_features(market()), "kamino")[START + 120 * DAY]
        self.assertAlmostEqual(f.utilisation, 0.66 / 1.66)
        self.assertAlmostEqual(f.utilisation_change, 0.0, places=12)

    def test_zero_or_missing_denominators_never_divide(self):
        data = market(days=150)
        k = data["kamino"]
        d = START + 100 * DAY
        k.borrowed[d] = 0.0
        del k.borrowed[d + DAY]
        res = by_day(compute_daily_features(data), "kamino")
        self.assertIsNone(res[d].utilisation)
        self.assertEqual(res[d].unavailable["utilisation"], "no_observation")
        self.assertIsNone(res[d + DAY].utilisation)
        self.assertEqual(res[d + DAY].unavailable["borrowed_log_change"], "no_observation")
        self.assertIsNone(res[d + 2 * DAY].utilisation_change)  # previous day had no utilisation

    def test_utilisation_jump_is_scored_against_its_own_trailing_baseline(self):
        data = market(days=200)
        k = data["kamino"]
        rng = np.random.default_rng(1)
        for day in sorted(k.borrowed):
            k.borrowed[day] *= 1 + rng.normal(0, 0.005)
        d = START + 150 * DAY
        k.tvl[d] *= 0.75  # deposits withdrawn, borrowing flat -> utilisation jumps
        f = by_day(compute_daily_features(data), "kamino")[d]
        self.assertGreater(f.utilisation_change, 0.05)
        self.assertGreater(f.utilisation_change_z, 10)

    def test_zero_dispersion_baseline(self):
        self.assertEqual(fd.robust_z(1.0, [0.0] * 90), (None, "zero_dispersion"))
        self.assertEqual(fd.robust_z(1.0, [0.1] * 10), (None, "insufficient_baseline"))
        z, reason = fd.robust_z(1.0, [0.0] * 85 + [0.5] * 5)  # MAD 0, mean AD > 0
        self.assertIsNone(reason)
        self.assertTrue(math.isfinite(z))


class InputAdapterTests(unittest.TestCase):
    def test_rows_become_inputs_without_mixing_units(self):
        d = START
        rows = [
            {"slug": "kamino", "metric": "tvl.usd.daily", "day": d, "symbol": None, "value": 1000},
            {"slug": "kamino", "metric": "tvl.borrowed.usd.daily", "day": d, "symbol": None, "value": 700},
            {"slug": "kamino", "metric": "tokens.qty.daily", "day": d, "symbol": "SOL", "value": 2},
            {"slug": "kamino", "metric": "tokens.usd.daily", "day": d, "symbol": "SOL", "value": 300},
            {"slug": "kamino", "metric": "tokens.qty.daily", "day": d, "symbol": "JUP", "value": 5},
            {"slug": "ghost", "metric": "tvl.usd.daily", "day": d, "symbol": None, "value": 1},
        ]
        p = inputs_from_rows([{"slug": "kamino", "category": "LENDING"}], rows)["kamino"]
        self.assertEqual((p.tvl[d], p.borrowed[d]), (1000.0, 700.0))
        self.assertEqual(p.tokens[d]["SOL"], AssetDay(2.0, 300.0))
        self.assertEqual(p.tokens[d]["JUP"], AssetDay(5.0, None))  # unpriced, never priced as 5 USD

    def test_version_and_constants(self):
        self.assertEqual(fd.FEATURE_VERSION, "features-daily-v1")
        self.assertEqual(fd.REGIME_BREAKS, {"drift": date(2026, 4, 2)})
        self.assertEqual((fd.BASELINE_DAYS, fd.MIN_BASELINE_POINTS, fd.DRAWDOWN_DAYS), (90, 60, 30))


if __name__ == "__main__":
    unittest.main()
