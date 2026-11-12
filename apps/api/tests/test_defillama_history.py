import os
import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
import httpx

from app.adapters import defillama_history as dl
from app.adapters.defillama_history import (
    ADAPTER_KEY,
    METRIC_TVL_DAILY,
    DailyPoint,
    HistoryUnavailable,
    fetch_protocol_payload,
    parse_daily_tvl,
)
from app.tasks import defillama_backfill
from app.tasks.defillama_backfill import plan_inserts, run_backfill

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)
DAY = 86_400


def ts(d: date, seconds: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + seconds


def payload(*points):
    return {"name": "x", "tvl": [{"date": d, "totalLiquidityUSD": v} for d, v in points]}


class ParseDailyTvlTests(unittest.TestCase):
    def test_keeps_only_closed_midnight_days_with_source_dates(self):
        result = parse_daily_tvl(payload(
            (ts(date(2026, 11, 9)), 100),
            (ts(date(2026, 11, 10)), 110),
            (ts(date(2026, 11, 11)), 120),
            (ts(date(2026, 11, 12)), 130),            # today 00:00: still open, excluded
            (ts(date(2026, 11, 12), 21_491), 131),    # intraday point, excluded
        ), NOW)
        self.assertEqual([p.day for p in result.points], [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)])
        self.assertEqual(result.points[0].observed_at, datetime(2026, 11, 9))  # naive UTC midnight
        self.assertIsNone(result.points[0].observed_at.tzinfo)
        self.assertEqual(result.points[2].value_usd, Decimal(120))
        self.assertEqual(result.source_points, 5)
        self.assertEqual(dict(result.excluded), {"open_day": 1, "intraday_point": 1})

    def test_open_day_depends_on_now_in_utc(self):
        late_evening_elsewhere = datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)
        result = parse_daily_tvl(payload((ts(date(2026, 11, 11)), 5)), late_evening_elsewhere)
        self.assertEqual(len(result.points), 1)
        result = parse_daily_tvl(payload((ts(date(2026, 11, 12)), 5)), late_evening_elsewhere)
        self.assertEqual(len(result.points), 0)

    def test_missing_days_are_not_filled(self):
        result = parse_daily_tvl(payload(
            (ts(date(2026, 11, 1)), 1), (ts(date(2026, 11, 2)), 2), (ts(date(2026, 11, 6)), 6),
        ), NOW)
        self.assertEqual(len(result.points), 3)
        self.assertEqual(result.missing_days, 3)  # 3rd, 4th, 5th
        self.assertNotIn(date(2026, 11, 3), [p.day for p in result.points])

    def test_invalid_values_are_dropped_never_zero(self):
        result = parse_daily_tvl(payload(
            (ts(date(2026, 11, 1)), 0), (ts(date(2026, 11, 2)), -5), (ts(date(2026, 11, 3)), None),
            (ts(date(2026, 11, 4)), float("nan")), (ts(date(2026, 11, 5)), "12"), (ts(date(2026, 11, 6)), True),
            (ts(date(2026, 11, 7)), 1.5),
        ), NOW)
        self.assertEqual([(p.day, p.value_usd) for p in result.points], [(date(2026, 11, 7), Decimal("1.5"))])
        self.assertEqual(result.excluded["invalid_value"], 6)

    def test_ambiguous_duplicate_day_keeps_neither(self):
        result = parse_daily_tvl(payload((ts(date(2026, 11, 1)), 1), (ts(date(2026, 11, 1)), 2), (ts(date(2026, 11, 2)), 3)), NOW)
        self.assertEqual([p.day for p in result.points], [date(2026, 11, 2)])
        self.assertEqual(result.excluded["duplicate_day"], 2)

    def test_malformed_payloads(self):
        for bad in (None, [], {"tvl": None}, {"no": "tvl"}):
            with self.assertRaises(HistoryUnavailable):
                parse_daily_tvl(bad, NOW)
        result = parse_daily_tvl({"tvl": ["x", {"date": "yesterday", "totalLiquidityUSD": 1}]}, NOW)
        self.assertEqual(result.excluded["malformed_point"], 2)

    def test_drift_uses_its_defillama_history_slug(self):
        self.assertEqual(dl.HISTORY_SLUGS["drift"], "drift-trade")
        self.assertEqual(dl.HISTORY_SLUGS["kamino"], "kamino-lend")


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_errors_become_short_codes(self):
        def handler(request):
            return httpx.Response(429, json={"error": "slow down"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(HistoryUnavailable) as ctx:
                await fetch_protocol_payload(client, "raydium")
        self.assertEqual(ctx.exception.code, "HTTP_429")


class PlanInsertsTests(unittest.TestCase):
    def test_only_missing_days_are_inserted_and_conflicts_are_not_overwritten(self):
        points = [DailyPoint(date(2026, 11, d), datetime(2026, 11, d), Decimal(d * 10)) for d in (1, 2, 3)]
        stored = {datetime(2026, 11, 1): Decimal("10.000000000000"), datetime(2026, 11, 2): Decimal(999)}
        to_insert, same, conflicts = plan_inserts(points, stored)
        self.assertEqual([p.day.day for p in to_insert], [3])
        self.assertEqual((same, conflicts), (1, 1))


def history_transport(series_by_llama_slug, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(slug)
        if slug not in series_by_llama_slug:
            return httpx.Response(404)
        return httpx.Response(200, json=payload(*series_by_llama_slug[slug]))
    return httpx.MockTransport(handler)


@unittest.skipUnless(
    os.environ.get("SOLANAGUARD_TEST_DATABASE_URL"),
    "set SOLANAGUARD_TEST_DATABASE_URL to a disposable, migrated database to run",
)
class BackfillDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Real PostgreSQL. Each test creates its own protocols, so runs never interfere."""

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["SOLANAGUARD_TEST_DATABASE_URL"], min_size=1, max_size=3)
        self.tag = uuid.uuid4().hex[:10]
        self.slugs = {f"t{self.tag}a": f"llama-{self.tag}-a", f"t{self.tag}b": f"llama-{self.tag}-b"}
        for slug in self.slugs:
            await self.pool.execute(
                'INSERT INTO protocols (id, slug, name, category, "programId", "isActive") VALUES ($1, $1, $1, \'DEX\', $1, true)', slug)
        patcher = patch.dict(dl.HISTORY_SLUGS, self.slugs)
        patcher.start()
        patcher2 = patch.dict(defillama_backfill.HISTORY_SLUGS, self.slugs)
        patcher2.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(patcher2.stop)

    async def asyncTearDown(self):
        # Leave the rows (observations are never deleted) but stop later runs picking them up.
        await self.pool.execute('UPDATE protocols SET "isActive" = false WHERE slug = ANY($1::text[])', list(self.slugs))
        await self.pool.close()

    def series(self, values_a, values_b):
        a, b = self.slugs.values()
        start = date(2026, 10, 1)
        return {
            a: [(ts(date.fromordinal(start.toordinal() + i)), v) for i, v in enumerate(values_a) if v is not None]
            + [(ts(date(2026, 11, 12), 3600), 1)],  # intraday point, never stored
            b: [(ts(date.fromordinal(start.toordinal() + i)), v) for i, v in enumerate(values_b) if v is not None],
        }

    async def backfill(self, series):
        async with httpx.AsyncClient(transport=history_transport(series)) as client:
            return await run_backfill(pool=self.pool, client=client, now=NOW, slugs=list(self.slugs))

    async def stored(self, slug):
        return await self.pool.fetch(
            'SELECT o."sourceObservedAt" AS day, o."valueNumeric" AS value, o."collectedAt" AS collected, o.source, '
            'o."adapterVersion" AS version, o."addressId" AS address, o.unit, o.status::text AS status '
            'FROM metric_observations o JOIN protocols p ON p.id = o."protocolId" '
            'WHERE p.slug = $1 AND o."adapterKey" = $2 AND o."metricKey" = $3 ORDER BY 1', slug, ADAPTER_KEY, METRIC_TVL_DAILY)

    async def test_backfill_is_idempotent_and_keeps_source_dates(self):
        series = self.series([100 + i for i in range(10)], [50, 51, None, None, 54])  # b has a 2-day gap
        first = await self.backfill(series)
        by_slug = {p.slug: p for p in first.protocols}
        a_slug, b_slug = self.slugs
        self.assertEqual((by_slug[a_slug].inserted, by_slug[a_slug].closed_days, by_slug[a_slug].verified), (10, 10, True))
        self.assertEqual(by_slug[a_slug].excluded, {"intraday_point": 1})
        self.assertEqual((by_slug[b_slug].inserted, by_slug[b_slug].missing_days_in_source), (3, 2))

        rows = await self.stored(a_slug)
        self.assertEqual(rows[0]["day"], datetime(2026, 10, 1))
        self.assertEqual(rows[-1]["day"], datetime(2026, 10, 10))
        self.assertEqual(rows[0]["value"], Decimal(100))
        self.assertTrue(all(r["source"] == "defillama" and r["version"] == "1" and r["address"] is None
                            and r["unit"] == "usd" and r["status"] == "OK" for r in rows))
        self.assertTrue(all(r["collected"] != r["day"] for r in rows))  # collection time kept separately
        b_days = [r["day"].day for r in await self.stored(b_slug)]
        self.assertEqual(b_days, [1, 2, 5])  # gap not interpolated

        second = await self.backfill(series)
        self.assertEqual(second.totals["inserted"], 0)
        self.assertEqual(second.totals["already_stored"], 13)
        self.assertTrue(all(p.verified for p in second.protocols))
        self.assertEqual(len(await self.stored(a_slug)), 10)

    async def test_new_days_are_appended_and_conflicts_are_reported_not_overwritten(self):
        await self.backfill(self.series([100, 101, 102], [7, 8, 9]))
        revised = self.series([100, 555, 102, 103], [7, 8, 9])  # day 2 revised at source, day 4 new
        report = await self.backfill(revised)
        a = next(p for p in report.protocols if p.slug == list(self.slugs)[0])
        self.assertEqual((a.inserted, a.already_stored, a.conflicts, a.verified), (1, 2, 1, False))
        values = {r["day"].day: r["value"] for r in await self.stored(a.slug)}
        self.assertEqual(values, {1: Decimal(100), 2: Decimal(101), 3: Decimal(102), 4: Decimal(103)})

    async def test_source_failure_is_isolated_and_reported(self):
        a, b = self.slugs.values()
        series = {a: self.series([1, 2], [1])[a]}  # b's slug returns 404
        report = await self.backfill(series)
        by_slug = {p.llama_slug: p for p in report.protocols}
        self.assertEqual(by_slug[b].error, "HTTP_404")
        self.assertTrue(by_slug[a].verified)
        self.assertEqual(report.totals["failed"], 1)


if __name__ == "__main__":
    unittest.main()
