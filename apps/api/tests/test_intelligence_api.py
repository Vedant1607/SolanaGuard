"""GET /api/v1/protocols/{slug}/intelligence contract. Series are synthetic test data."""

import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ml import tvl_robust
from app.routers import intelligence
from app.routers.intelligence import PROTOCOL_QUERY, SERIES_QUERY, router
from tests.test_tvl_robust import synthetic_market

TODAY = datetime.now(timezone.utc).date()


def rows_from(series, end: date):
    """Shift a synthetic series so its last day is ``end`` and shape it like SERIES_QUERY rows."""
    out = []
    for slug, observations in series.items():
        shift = end - observations[-1].day
        for o in observations:
            day = o.day + shift
            out.append({
                "id": f"{slug}:{day.isoformat()}", "slug": slug, "observed_at": datetime(day.year, day.month, day.day),
                "value": Decimal(str(round(o.value, 2))), "collected_at": datetime(2026, 11, 12, 9, 0),
            })
    return out


class FakeConnection:
    def __init__(self, protocol, rows, error=None):
        self.protocol, self.rows, self.error = protocol, rows, error
        self.args = None

    async def fetchrow(self, query, *args):
        assert query == PROTOCOL_QUERY
        if self.error:
            raise self.error
        return self.protocol

    async def fetch(self, query, *args):
        assert query == SERIES_QUERY
        self.args = args
        return self.rows


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Ctx()


def call(conn, slug="p0", query=""):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/protocols")
    async def pool():
        return FakePool(conn)
    with patch.object(intelligence, "get_pool", pool):
        return TestClient(app).get(f"/api/v1/protocols/{slug}/intelligence{query}")


PROTOCOL = {"id": "p0", "slug": "p0", "name": "P0", "is_active": True}


class IntelligenceApiTests(unittest.TestCase):
    def test_protocol_specific_drop_is_unusual_with_provenance(self):
        series = synthetic_market(days=200, shocks={("p0", 199): -0.35})
        conn = FakeConnection(PROTOCOL, rows_from(series, TODAY - timedelta(days=1)))
        res = call(conn)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["classification"], "UNUSUAL")
        self.assertEqual(body["model"]["version"], "tvl-robust-v1")
        self.assertEqual(body["threshold"], tvl_robust.THRESHOLD)
        self.assertLessEqual(body["robust_z"], -tvl_robust.THRESHOLD)
        self.assertEqual(body["direction"], "decline")
        self.assertFalse(body["market"]["market_wide"])
        self.assertEqual(body["latest_closed_day"], (TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(body["latest_age_days"], 1)
        self.assertEqual(body["source"], {"source": "defillama", "adapter": "defillama_tvl_history@1",
                                          "metric_key": "tvl.usd.daily", "resolution": "closed UTC days"})
        support = {s["role"]: s for s in body["supporting_observations"]}
        self.assertEqual(support["latest_day"]["id"], f"p0:{(TODAY - timedelta(days=1)).isoformat()}")
        self.assertEqual(support["previous_day"]["id"], f"p0:{(TODAY - timedelta(days=2)).isoformat()}")
        self.assertEqual(body["observation_window"]["baseline_points"], 90)
        self.assertEqual(body["observation_window"]["peers"], 4)
        self.assertTrue(body["explanation"])
        self.assertEqual(body["insufficient_reasons"], [])
        self.assertIn("not a probability", body["disclaimer"])
        self.assertEqual(conn.args[:2], ("defillama_tvl_history", "tvl.usd.daily"))
        self.assertEqual(conn.args[3], "p0")  # the protocol is always included, even if inactive

    def test_market_wide_drop_is_normal_and_labelled_market_wide(self):
        shocks = {(f"p{p}", 199): -0.35 for p in range(5)}
        body = call(FakeConnection(PROTOCOL, rows_from(synthetic_market(days=200, shocks=shocks), TODAY - timedelta(days=1)))).json()
        self.assertEqual(body["classification"], "NORMAL")
        self.assertTrue(body["market"]["market_wide"])

    def test_no_observations_is_insufficient_history_with_reason(self):
        body = call(FakeConnection({**PROTOCOL, "is_active": False}, [])).json()
        self.assertEqual(body["classification"], "INSUFFICIENT_HISTORY")
        self.assertEqual([r["code"] for r in body["insufficient_reasons"]], ["no_observations"])
        self.assertIsNone(body["robust_z"])
        self.assertIsNone(body["latest_closed_day"])
        self.assertEqual(body["supporting_observations"], [])

    def test_short_history_explains_itself_and_never_reports_a_score(self):
        body = call(FakeConnection(PROTOCOL, rows_from(synthetic_market(days=40), TODAY - timedelta(days=1)))).json()
        self.assertEqual(body["classification"], "INSUFFICIENT_HISTORY")
        self.assertIn("insufficient_history", [r["code"] for r in body["insufficient_reasons"]])
        self.assertIsNone(body["robust_z"])

    def test_stale_history_is_not_reported_as_current(self):
        body = call(FakeConnection(PROTOCOL, rows_from(synthetic_market(days=200), TODAY - timedelta(days=10)))).json()
        self.assertEqual(body["classification"], "INSUFFICIENT_HISTORY")
        self.assertIn("stale_history", [r["code"] for r in body["insufficient_reasons"]])

    def test_unknown_protocol_is_404(self):
        self.assertEqual(call(FakeConnection(None, [])).status_code, 404)

    def test_missing_schema_is_503(self):
        res = call(FakeConnection(PROTOCOL, [], error=asyncpg.exceptions.UndefinedTableError("x")))
        self.assertEqual(res.status_code, 503)


class IntelligenceHistoryTests(unittest.TestCase):
    def test_history_is_opt_in_and_the_default_response_is_unchanged(self):
        body = call(FakeConnection(PROTOCOL, rows_from(synthetic_market(days=200), TODAY - timedelta(days=1)))).json()
        self.assertEqual(body["history"], [])
        self.assertIsNone(body["history_window"])

    def test_history_lists_stored_days_only_with_the_same_scores_as_the_model(self):
        end = TODAY - timedelta(days=1)
        series = synthetic_market(days=300, shocks={("p0", 290): -0.35}, skip={"p0": {280, 281}})
        rows = rows_from(series, end)
        body = call(FakeConnection(PROTOCOL, rows), query="?history_days=30").json()
        window = body["history_window"]
        self.assertEqual((window["start"], window["end"], window["days"]),
                         ((end - timedelta(days=29)).isoformat(), end.isoformat(), 30))
        days = [h["day"] for h in body["history"]]
        self.assertEqual(len(days), 28)  # 30 calendar days, 2 missing at source: not listed, not filled
        self.assertEqual(days, sorted(days))
        missing = {(end - timedelta(days=299 - t)).isoformat() for t in (280, 281)}
        self.assertFalse(missing & set(days))
        after_gap = next(h for h in body["history"] if h["day"] == (end - timedelta(days=299 - 282)).isoformat())
        self.assertIsNone(after_gap["excess_log_return"])
        self.assertIn("previous_day_missing", after_gap["reasons"])
        self.assertFalse(after_gap["unusual"])

        shocked = next(h for h in body["history"] if h["day"] == (end - timedelta(days=9)).isoformat())
        self.assertTrue(shocked["unusual"])
        self.assertLessEqual(shocked["robust_z"], -tvl_robust.THRESHOLD)
        self.assertEqual(shocked["observation_id"], f"p0:{shocked['day']}")

        # Same numbers as the model run directly on the same observations.
        shifted = {slug: [tvl_robust.DailyObservation(r["observed_at"].date(), float(r["value"]), r["id"])
                          for r in rows if r["slug"] == slug] for slug in series}
        direct = {s.day.isoformat(): s for s in tvl_robust.compute_scores(shifted)["p0"]}
        for h in body["history"]:
            self.assertEqual(h["robust_z"], direct[h["day"]].robust_z)
            self.assertEqual(h["excess_log_return"], direct[h["day"]].excess_return)
            self.assertEqual(h["unusual"], direct[h["day"]].robust_z is not None and abs(direct[h["day"]].robust_z) >= tvl_robust.THRESHOLD)

    def test_history_days_is_bounded(self):
        conn = FakeConnection(PROTOCOL, [])
        self.assertEqual(call(conn, query=f"?history_days={intelligence.MAX_HISTORY_DAYS + 1}").status_code, 422)
        self.assertEqual(call(conn, query="?history_days=-1").status_code, 422)
        self.assertEqual(call(conn, query=f"?history_days={intelligence.MAX_HISTORY_DAYS}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
