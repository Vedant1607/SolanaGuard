"""defillama_token_history@1: parsing and the idempotent backfill. Payloads are hand-built fixtures."""

import os
import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
import httpx

from app.adapters import defillama_tokens as dt
from app.adapters.defillama_history import HistoryUnavailable
from app.adapters.defillama_tokens import (
    ADAPTER_KEY,
    METRIC_BORROWED_USD,
    METRIC_TOKEN_QTY,
    METRIC_TOKEN_USD,
    parse_borrowed_history,
    parse_token_history,
)
from app.tasks import defillama_token_backfill as tb
from app.tasks.defillama_token_backfill import Planned, plan_metric, run_token_backfill

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)


def ts(d: date, seconds: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + seconds


def payload(qty, usd=None, borrowed=None):
    """qty/usd: [(date or ts, {symbol: value})]. borrowed: [(date or ts, usd)]."""
    stamp = lambda d: d if isinstance(d, int) else ts(d)
    chains = {"Solana": {"tvl": [], "tokens": [{"date": stamp(d), "tokens": t} for d, t in qty],
                         "tokensInUsd": [{"date": stamp(d), "tokens": t} for d, t in (usd or [])]}}
    if borrowed is not None:
        chains["Solana-borrowed"] = {"tvl": [{"date": stamp(d), "totalLiquidityUSD": v} for d, v in borrowed]}
    return {"name": "x", "tvl": [], "chainTvls": chains}


D1, D2, D3, D4 = date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)


class ParseTokenHistoryTests(unittest.TestCase):
    def test_multiple_assets_keep_their_own_quantities_and_usd_values(self):
        result = parse_token_history(payload(
            [(D1, {"SOL": 100.5, "USDC": 2000}), (D2, {"SOL": 101, "USDC": 1500, "JITOSOL": 3})],
            [(D1, {"SOL": 15075.0, "USDC": 2000.0}), (D2, {"SOL": 15150.0, "USDC": 1500.0})],
        ), NOW)
        got = {(p.day, p.symbol): (p.quantity, p.value_usd) for p in result.points}
        self.assertEqual(got[(D1, "SOL")], (Decimal("100.5"), Decimal("15075.0")))
        self.assertEqual(got[(D2, "USDC")], (Decimal(1500), Decimal("1500.0")))
        self.assertEqual(got[(D2, "JITOSOL")], (Decimal(3), None))  # no USD given: quantity kept, never priced
        self.assertEqual(result.unpriced, 1)
        self.assertEqual(result.symbols, ["JITOSOL", "SOL", "USDC"])
        self.assertTrue(all(p.observed_at == datetime(p.day.year, p.day.month, p.day.day) and p.observed_at.tzinfo is None for p in result.points))

    def test_open_day_and_intraday_points_are_excluded(self):
        result = parse_token_history(payload([(D4, {"SOL": 1}), (date(2026, 11, 12), {"SOL": 2}), (ts(date(2026, 11, 12), 3600), {"SOL": 3})]), NOW)
        self.assertEqual([p.day for p in result.points], [D4])
        self.assertEqual((result.excluded["open_day"], result.excluded["intraday_point"]), (1, 1))

    def test_invalid_zero_and_negative_quantities_are_counted_never_stored(self):
        result = parse_token_history(payload([(D1, {"A": 0, "B": -5, "C": None, "D": float("nan"), "E": "7", "F": True, "G": 2.5})]), NOW)
        self.assertEqual([(p.symbol, p.quantity) for p in result.points], [("G", Decimal("2.5"))])
        self.assertEqual(result.excluded["zero_quantity"], 1)
        self.assertEqual(result.excluded["negative_quantity"], 1)
        self.assertEqual(result.excluded["invalid_quantity"], 4)

    def test_zero_or_negative_usd_leaves_the_asset_unpriced(self):
        result = parse_token_history(payload([(D1, {"A": 5, "B": 6})], [(D1, {"A": 0, "B": -1})]), NOW)
        self.assertEqual([p.value_usd for p in result.points], [None, None])
        self.assertEqual(result.unpriced, 2)

    def test_symbols_are_kept_as_reported_and_bad_symbols_rejected(self):
        result = parse_token_history(payload([(D1, {"SOL": 1, "WSOL": 2, " PUDGY PENGUINS ": 3, "": 4, "X" * 65: 5})]), NOW)
        self.assertEqual(sorted(p.symbol for p in result.points), ["PUDGY PENGUINS", "SOL", "WSOL"])
        self.assertEqual(result.excluded["invalid_symbol"], 2)

    def test_missing_days_stay_missing(self):
        result = parse_token_history(payload([(D1, {"SOL": 1}), (D4, {"SOL": 2})]), NOW)
        self.assertEqual(result.days, [D1, D4])
        self.assertEqual(result.missing_days, 2)

    def test_ambiguous_duplicate_day_keeps_neither(self):
        result = parse_token_history(payload([(D1, {"SOL": 1}), (D1, {"SOL": 2}), (D2, {"SOL": 3})]), NOW)
        self.assertEqual(result.days, [D2])
        self.assertEqual(result.excluded["duplicate_quantity_day"], 1)

    def test_malformed_payloads(self):
        for bad in (None, {}, {"chainTvls": None}, {"chainTvls": {"Solana": []}}):
            with self.assertRaises(HistoryUnavailable):
                parse_token_history(bad, NOW)
        with self.assertRaises(HistoryUnavailable) as ctx:
            parse_token_history({"chainTvls": {"Ethereum": {}}}, NOW)
        self.assertEqual(ctx.exception.code, "NO_CHAIN_SERIES")
        with self.assertRaises(HistoryUnavailable) as ctx:
            parse_token_history({"chainTvls": {"Solana": {"tvl": []}}}, NOW)
        self.assertEqual(ctx.exception.code, "NO_TOKEN_SERIES")
        result = parse_token_history({"chainTvls": {"Solana": {"tokens": ["x", {"date": "y", "tokens": {}}, {"date": ts(D1)}]}}}, NOW)
        self.assertEqual(result.points, [])
        self.assertEqual(result.excluded["malformed_quantity_point"], 2)
        self.assertEqual(result.excluded["malformed_point"], 1)

    def test_target_protocols(self):
        self.assertEqual(set(dt.TOKEN_HISTORY_SLUGS), {"jito", "marinade", "kamino", "drift"})
        self.assertEqual(dt.TOKEN_HISTORY_SLUGS["drift"], "drift-trade")
        self.assertNotIn("marginfi", dt.TOKEN_HISTORY_SLUGS)
        self.assertEqual(dt.BORROWED_HISTORY_SLUGS, {"kamino"})


class ParseBorrowedTests(unittest.TestCase):
    def test_borrowed_usd_closed_days_only(self):
        result = parse_borrowed_history(payload([], borrowed=[(D1, 900.5), (D2, 0), (D3, -1), (date(2026, 11, 12), 5)]), NOW)
        self.assertEqual([(p.day, p.value_usd) for p in result.points], [(D1, Decimal("900.5"))])
        self.assertEqual(result.excluded["invalid_value"], 2)
        self.assertEqual(result.excluded["open_day"], 1)

    def test_no_borrowed_series(self):
        with self.assertRaises(HistoryUnavailable) as ctx:
            parse_borrowed_history(payload([]), NOW)
        self.assertEqual(ctx.exception.code, "NO_CHAIN_SERIES")


class PlanTests(unittest.TestCase):
    def test_only_missing_keys_are_inserted_and_conflicts_never_overwrite(self):
        at = datetime(2026, 11, 8)
        planned = [Planned(METRIC_TOKEN_QTY, "token", at, s, v) for s, v in (("SOL", Decimal(1)), ("USDC", Decimal(2)), ("JITOSOL", Decimal(3)))]
        stored = {(at, "SOL"): Decimal("1.000000000000"), (at, "USDC"): Decimal(99)}
        to_insert, same, conflicts = plan_metric(planned, stored)
        self.assertEqual([p.symbol for p in to_insert], ["JITOSOL"])
        self.assertEqual((same, conflicts), (1, 1))

    def test_build_plan_assigns_units_and_metrics(self):
        result = tb.ProtocolTokenBackfill(slug="kamino", llama_slug="kamino-lend")
        plan = tb.build_plan("kamino", payload([(D1, {"SOL": 2, "X": 1})], [(D1, {"SOL": 300.0})], borrowed=[(D1, 50.0)]), NOW, result)
        self.assertEqual({p.unit for p in plan[METRIC_TOKEN_QTY]}, {"token"})
        self.assertEqual([(p.symbol, p.value) for p in plan[METRIC_TOKEN_USD]], [("SOL", Decimal("300.0"))])
        self.assertEqual([(p.symbol, p.value, p.unit) for p in plan[METRIC_BORROWED_USD]], [(None, Decimal("50.0"), "usd")])
        self.assertEqual(result.unpriced_asset_days, 1)
        jito = tb.build_plan("jito", payload([(D1, {"SOL": 2})]), NOW, tb.ProtocolTokenBackfill(slug="jito", llama_slug="j"))
        self.assertNotIn(METRIC_BORROWED_USD, jito)


def transport(payloads, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(slug)
        return httpx.Response(200, json=payloads[slug]) if slug in payloads else httpx.Response(429)
    return httpx.MockTransport(handler)


@unittest.skipUnless(
    os.environ.get("SOLANAGUARD_TEST_DATABASE_URL"),
    "set SOLANAGUARD_TEST_DATABASE_URL to a disposable database with the SolanaGuard schema to run",
)
class TokenBackfillDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Real PostgreSQL. Each test creates its own protocols, so runs never interfere."""

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["SOLANAGUARD_TEST_DATABASE_URL"], min_size=1, max_size=3)
        tag = uuid.uuid4().hex[:10]
        self.lend, self.lst = f"t{tag}lend", f"t{tag}lst"
        self.llama = {self.lend: f"llama-{tag}-lend", self.lst: f"llama-{tag}-lst"}
        for slug in self.llama:
            await self.pool.execute('INSERT INTO protocols (id, slug, name, category, "programId", "isActive") VALUES ($1, $1, $1, \'LENDING\', $1, true)', slug)
        for target in (dt.TOKEN_HISTORY_SLUGS, tb.TOKEN_HISTORY_SLUGS):
            p = patch.dict(target, self.llama)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(tb, "BORROWED_HISTORY_SLUGS", frozenset({self.lend}))
        p.start()
        self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.pool.execute('UPDATE protocols SET "isActive" = false WHERE slug = ANY($1::text[])', list(self.llama))
        await self.pool.close()

    def payloads(self, lend_qty, lend_usd, borrowed, lst_qty):
        return {self.llama[self.lend]: payload(lend_qty, lend_usd, borrowed), self.llama[self.lst]: payload(lst_qty)}

    async def run_once(self, payloads):
        async with httpx.AsyncClient(transport=transport(payloads)) as client:
            return await run_token_backfill(pool=self.pool, client=client, now=NOW, slugs=list(self.llama))

    async def rows(self, slug):
        return await self.pool.fetch(
            'SELECT o."metricKey" AS metric, o."sourceObservedAt" AS day, o."valueText" AS symbol, o."valueNumeric" AS value, '
            'o.unit, o.source, o."adapterKey" AS adapter, o."adapterVersion" AS version, o.status::text AS status, '
            'o."addressId" AS address, o."collectedAt" AS collected '
            'FROM metric_observations o JOIN protocols p ON p.id = o."protocolId" WHERE p.slug = $1 AND o."adapterKey" = $2 '
            'ORDER BY 1, 2, 3', slug, ADAPTER_KEY)

    async def test_backfill_is_idempotent_with_correct_units_and_provenance(self):
        payloads = self.payloads(
            [(D1, {"SOL": 10, "USDC": 500, "DUST": 0}), (D2, {"SOL": 11, "USDC": -3})],
            [(D1, {"SOL": 1500.0, "USDC": 500.0}), (D2, {"SOL": 1600.0})],
            [(D1, 700.0), (D2, 710.0)],
            [(D1, {"SOL": 1000}), (D3, {"SOL": 1100})],  # D2 missing at source
        )
        first = await self.run_once(payloads)
        self.assertEqual(first.totals["failed"], 0)
        self.assertEqual(first.totals["unverified"], 0)
        lend = next(p for p in first.protocols if p.slug == self.lend)
        by_metric = {m.metric: m for m in lend.metrics}
        self.assertEqual((by_metric[METRIC_TOKEN_QTY].inserted, by_metric[METRIC_TOKEN_USD].inserted, by_metric[METRIC_BORROWED_USD].inserted), (3, 3, 2))
        self.assertEqual(lend.excluded, {"zero_quantity": 1, "negative_quantity": 1})

        rows = await self.rows(self.lend)
        units = {(r["metric"], r["unit"]) for r in rows}
        self.assertEqual(units, {(METRIC_TOKEN_QTY, "token"), (METRIC_TOKEN_USD, "usd"), (METRIC_BORROWED_USD, "usd")})
        self.assertTrue(all(r["source"] == "defillama" and r["version"] == "1" and r["status"] == "OK" and r["address"] is None for r in rows))
        self.assertTrue(all(r["value"] > 0 for r in rows))  # no zero or negative quantity stored
        self.assertEqual({r["symbol"] for r in rows if r["metric"] == METRIC_BORROWED_USD}, {None})
        qty = {(r["day"].day, r["symbol"]): r["value"] for r in rows if r["metric"] == METRIC_TOKEN_QTY}
        self.assertEqual(qty, {(8, "SOL"): Decimal(10), (8, "USDC"): Decimal(500), (9, "SOL"): Decimal(11)})
        lst_days = [r["day"].day for r in await self.rows(self.lst)]
        self.assertEqual(lst_days, [8, 10])  # the source gap is not filled

        second = await self.run_once(payloads)
        self.assertEqual(second.totals["inserted"], 0)
        self.assertEqual(second.totals["already_stored"], first.totals["inserted"])
        self.assertEqual(len(await self.rows(self.lend)), len(rows))

    async def test_new_days_and_assets_append_and_revisions_are_conflicts_not_overwrites(self):
        await self.run_once(self.payloads([(D1, {"SOL": 10})], [(D1, {"SOL": 1500.0})], [(D1, 700.0)], [(D1, {"SOL": 1})]))
        revised = self.payloads([(D1, {"SOL": 12}), (D2, {"SOL": 11, "JUP": 4})], [(D1, {"SOL": 1500.0})], [(D1, 700.0), (D2, 720.0)], [(D1, {"SOL": 1})])
        report = await self.run_once(revised)
        lend = next(p for p in report.protocols if p.slug == self.lend)
        qty = next(m for m in lend.metrics if m.metric == METRIC_TOKEN_QTY)
        self.assertEqual((qty.inserted, qty.conflicts, qty.verified), (2, 1, False))
        stored = {(r["day"].day, r["symbol"]): r["value"] for r in await self.rows(self.lend) if r["metric"] == METRIC_TOKEN_QTY}
        self.assertEqual(stored[(8, "SOL")], Decimal(10))  # original kept
        self.assertEqual(stored[(9, "JUP")], Decimal(4))

    async def test_source_failure_is_isolated(self):
        payloads = {self.llama[self.lst]: payload([(D1, {"SOL": 1})])}  # lending slug answers 429
        report = await self.run_once(payloads)
        by = {p.slug: p for p in report.protocols}
        self.assertEqual(by[self.lend].error, "HTTP_429")
        self.assertTrue(by[self.lst].verified)
        self.assertEqual(await self.rows(self.lend), [])


if __name__ == "__main__":
    unittest.main()
