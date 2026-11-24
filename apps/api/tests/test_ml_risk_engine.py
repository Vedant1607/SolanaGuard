"""Category ML risk engine (PR #1) and its independent ingestion step."""

import os
import unittest
import uuid
import warnings
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import numpy as np

from app.ml import engine
from app.solana import protocols
from app.tasks import ingest, ml_risk

T0 = datetime(2026, 11, 1, 0, 0, 0)
API_DIR = Path(__file__).resolve().parents[1]


def snapshot_rows(count=60, *, tx=True, gap_at=None, spike=False, seed=7):
    """Chronological snapshot dicts shaped like the ML query's rows. Only TVL and
    transaction count are populated, as in production ingestion."""
    rng = np.random.default_rng(seed)
    rows = []
    at = T0
    for i in range(count):
        if gap_at is not None and i == gap_at:
            at += timedelta(hours=2)
        tx_count = int(5_000 + rng.normal(0, 50)) if tx else None
        if spike and i == count - 1:
            tx_count = 50_000
        rows.append({
            "id": f"snap-{i}",
            "tvlUsd": float(100_000_000 + rng.normal(0, 200_000)),
            "volume24hUsd": 0.0,
            "txCount24h": tx_count,
            "uniqueWallets24h": 0,
            "liquidityDepth": 0.0,
            "utilizationRate": 0.0,
            "snapshotAt": at,
        })
        at += timedelta(minutes=5)
    return rows


def engine_input(rows):
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeMlConnection:
    def __init__(self, category="DEX", rows=None):
        self.category = category
        self.rows = rows or []  # chronological
        self.executed = []

    async def fetchval(self, query, *args):
        return self.category

    async def fetch(self, query, *args):
        return list(reversed(self.rows))  # query orders newest first

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"

    def transaction(self):
        return Transaction()


def ml_result(anomaly_type="TX_SPIKE", is_anomaly=True):
    return {
        "protocol_slug": "raydium",
        "overall_score": 41.2,
        "risk_level": "MEDIUM",
        "components": {"liquidity_score": 100.0, "anomaly_score": 80.0},
        "confidence": 0.82,
        "method": "ml",
        "anomaly": {
            "is_anomaly": is_anomaly,
            "score": 80.0,
            "type": anomaly_type if is_anomaly else None,
            "severity": "HIGH" if is_anomaly else None,
            "description": "desc" if is_anomaly else None,
        },
    }


class RiskEngineTests(unittest.TestCase):
    def test_committed_models_load_without_version_warnings(self):
        engine.load_model.cache_clear()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for category in engine.MODEL_FILES:
                model = engine.load_model(category)
                self.assertEqual(model.n_features_in_, 11)

    def test_models_are_loaded_once(self):
        engine.load_model.cache_clear()
        with patch("app.ml.engine.joblib.load", return_value=object()) as load:
            engine.load_model("DEX")
            engine.load_model("DEX")
        self.assertEqual(load.call_count, 1)
        engine.load_model.cache_clear()

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(ValueError):
            engine.load_model("PERPS")

    def test_scores_protocol_with_ml_output_contract(self):
        result = engine.score_protocol("DEX", "raydium", engine_input(snapshot_rows()))
        self.assertEqual(result["method"], "ml")
        self.assertIn(result["risk_level"], {"LOW", "MEDIUM", "HIGH", "CRITICAL"})
        self.assertTrue(0 <= result["overall_score"] <= 100)
        self.assertTrue(0 <= result["anomaly"]["score"] <= 100)
        self.assertEqual(
            set(result["components"]),
            {"liquidity_score", "smart_contract_score", "market_score",
             "governance_score", "sentiment_score", "anomaly_score"},
        )

    def test_characterization_unmeasured_liquidity_maxes_liquidity_component(self):
        # Ingestion never collects liquidityDepth (stored as 0), so PR #1's
        # liquidity sub-score is always 100. This pins the known limitation
        # that keeps ML scores out of the displayed and alerting score.
        result = engine.score_protocol("DEX", "raydium", engine_input(snapshot_rows()))
        self.assertEqual(result["components"]["liquidity_score"], 100.0)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(engine.score_protocol("DEX", "raydium", engine_input(snapshot_rows(24))))

    def test_importing_detector_does_not_create_directories(self):
        import importlib

        import app.ml.category_anomaly_detector as detector

        importlib.reload(detector)
        self.assertFalse((API_DIR / "app" / "models").exists())

    def test_detector_modules_are_distinct(self):
        import app.ml.anomaly_detector as time_window
        import app.ml.category_anomaly_detector as category

        self.assertEqual(time_window.DETECTOR_VERSION, "iforest-v2-time-window")
        self.assertNotEqual(ml_risk.ML_DETECTOR_VERSION, time_window.DETECTOR_VERSION)
        self.assertIs(engine.detect, category.detect)


class AssessMlRiskTests(unittest.IsolatedAsyncioTestCase):
    async def assess(self, conn, snapshot_id=None):
        snapshot_id = snapshot_id or (conn.rows[-1]["id"] if conn.rows else "snap-x")
        return await ml_risk.assess_ml_risk(conn, "protocol-1", "raydium", snapshot_id)

    async def test_unsupported_category_writes_nothing(self):
        conn = FakeMlConnection(category="PERPS", rows=snapshot_rows())
        outcome = await self.assess(conn)
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "unsupported_category"))
        self.assertEqual(conn.executed, [])

    async def test_insufficient_history_is_a_skip_not_a_normal_result(self):
        conn = FakeMlConnection(rows=snapshot_rows(24))
        outcome = await self.assess(conn)
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "insufficient_history"))
        self.assertEqual(conn.executed, [])

    async def test_missing_transaction_counts_are_not_treated_as_zero(self):
        rows = snapshot_rows()
        rows[10]["txCount24h"] = None
        conn = FakeMlConnection(rows=rows)
        with patch("app.tasks.ml_risk.score_protocol") as score:
            outcome = await self.assess(conn)
        score.assert_not_called()
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "tx_count_unavailable"))
        self.assertEqual(conn.executed, [])

    async def test_gap_in_rolling_window_is_skipped(self):
        conn = FakeMlConnection(rows=snapshot_rows(60, gap_at=50))
        outcome = await self.assess(conn)
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "history_gap"))
        self.assertEqual(conn.executed, [])

    async def test_old_gap_outside_rolling_window_does_not_block_scoring(self):
        conn = FakeMlConnection(rows=snapshot_rows(60, gap_at=5))
        outcome = await self.assess(conn)
        self.assertEqual(outcome.status, "scored")

    async def test_stale_snapshot_is_not_scored(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        outcome = await self.assess(conn, snapshot_id="some-other-snapshot")
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "stale_snapshot"))
        self.assertEqual(conn.executed, [])

    async def test_engine_returning_none_writes_nothing(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        with patch("app.tasks.ml_risk.score_protocol", return_value=None):
            outcome = await self.assess(conn)
        self.assertEqual((outcome.status, outcome.reason), ("skipped", "insufficient_features"))
        self.assertEqual(conn.executed, [])

    async def test_engine_failure_propagates_without_writes(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        with patch("app.tasks.ml_risk.score_protocol", side_effect=RuntimeError("model broke")):
            with self.assertRaises(RuntimeError):
                await self.assess(conn)
        self.assertEqual(conn.executed, [])

    async def test_real_engine_persists_ml_risk_row_with_provenance(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        outcome = await self.assess(conn)
        self.assertEqual(outcome.status, "scored")
        self.assertTrue(outcome.risk_score_written)
        query, args = conn.executed[0]
        self.assertIn("INSERT INTO risk_scores", query)
        self.assertEqual(args[4], "ml")
        self.assertIn(ml_risk.ML_DETECTOR_VERSION, args[5])
        self.assertIn("volume24hUsd,uniqueWallets24h,liquidityDepth,utilizationRate", args[5])
        self.assertEqual(args[6], conn.rows[-1]["snapshotAt"])

    async def test_engine_receives_bounded_history(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        fetch = AsyncMock(return_value=list(reversed(conn.rows)))
        conn.fetch = fetch
        await self.assess(conn)
        self.assertEqual(fetch.call_args.args[2], ml_risk.ML_RISK_HISTORY_LIMIT)

    async def test_supported_anomaly_is_persisted_under_its_own_identity(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result("TX_SPIKE")):
            outcome = await self.assess(conn)
        self.assertTrue(outcome.anomaly_written)
        query, args = conn.executed[1]
        self.assertIn("INSERT INTO anomalies", query)
        self.assertIn("ON CONFLICT", query)
        self.assertEqual(args[2], conn.rows[-1]["id"])  # sourceSnapshotId
        self.assertEqual(args[3], ml_risk.ML_DETECTOR_VERSION)
        self.assertEqual(args[4], ml_risk.ML_DETECTION_METHOD)
        self.assertEqual(args[5], "TX_SPIKE")
        self.assertEqual(args[8], ml_risk.ML_ANOMALY_SCORE_THRESHOLD)

    async def test_anomaly_types_from_unmeasured_inputs_are_not_persisted(self):
        for anomaly_type, missing in (
            ("WHALE_MOVEMENT", "uniqueWallets24h"),
            ("PRICE_DEVIATION", "volume24hUsd,liquidityDepth"),
            ("LIQUIDITY_WITHDRAWAL", "liquidityDepth"),
        ):
            with self.subTest(anomaly_type=anomaly_type):
                conn = FakeMlConnection(rows=snapshot_rows())
                with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result(anomaly_type)):
                    outcome = await self.assess(conn)
                self.assertFalse(outcome.anomaly_written)
                self.assertEqual(outcome.anomaly_not_persisted_reason, f"unmeasured_inputs:{missing}")
                self.assertEqual(len(conn.executed), 1)  # risk score only
                self.assertIn("INSERT INTO risk_scores", conn.executed[0][0])

    async def test_anomaly_type_persists_once_its_inputs_are_measured(self):
        rows = snapshot_rows()
        for row in rows:
            row["uniqueWallets24h"] = 900
        conn = FakeMlConnection(rows=rows)
        with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result("WHALE_MOVEMENT")):
            outcome = await self.assess(conn)
        self.assertTrue(outcome.anomaly_written)

    async def test_non_anomalous_result_writes_only_the_risk_score(self):
        conn = FakeMlConnection(rows=snapshot_rows())
        with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result(is_anomaly=False)):
            outcome = await self.assess(conn)
        self.assertEqual([q for q, _ in conn.executed if "anomalies" in q], [])
        self.assertTrue(outcome.risk_score_written)


class IngestFakeConnection:
    """One protocol's ingestion cycle: rule-based history (LIMIT 2) and ML history."""

    def __init__(self, category="DEX"):
        self.category = category
        self.executed = []
        self.fetchrow_queries = []
        self.snapshot_id = None

    async def fetchval(self, query, *args):
        if "category" in query:
            return self.category
        return "protocol-1"

    async def fetch(self, query, *args):
        if "LIMIT 2" in query:
            return [{"tvlUsd": 100.0, "txCount24h": 5_000}, {"tvlUsd": 100.0, "txCount24h": 5_000}]
        rows = snapshot_rows()
        rows[-1]["id"] = self.snapshot_id
        return list(reversed(rows))

    async def fetchrow(self, query, *args):
        self.fetchrow_queries.append(query)
        return None

    async def execute(self, query, *args):
        if "INSERT INTO protocol_snapshots" in query:
            self.snapshot_id = args[0]
        self.executed.append((query, args))
        return "INSERT 0 1"

    def transaction(self):
        return Transaction()


class IngestPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return None

        return Acquire()


def risk_methods(conn):
    return [args[4] for query, args in conn.executed if "INSERT INTO risk_scores" in query]


class IngestionIndependenceTests(unittest.IsolatedAsyncioTestCase):
    async def run_cycle(self, conn, **patches):
        metrics = protocols.ProtocolMetrics(slug="raydium", tvl_usd=100.0, tx_count_24h=5_000)
        with ExitStack() as stack:
            stack.enter_context(patch("app.tasks.ingest.LAUNCH_PROTOCOLS", {"raydium": {}}))
            stack.enter_context(patch("app.tasks.ingest.get_protocol_metrics", AsyncMock(return_value=metrics)))
            for target, kwargs in patches.items():
                stack.enter_context(patch(target, **kwargs))
            return await ingest._ingest_protocols(IngestPool(conn))

    async def test_both_scores_are_written_with_distinct_methods(self):
        conn = IngestFakeConnection()
        self.assertEqual(await self.run_cycle(conn), 1)
        self.assertEqual(risk_methods(conn), ["rule_based", "ml"])

    async def test_ml_failure_keeps_rule_based_score_and_snapshot(self):
        conn = IngestFakeConnection()
        with self.assertLogs("app.tasks.ingest", level="ERROR") as logs:
            written = await self.run_cycle(
                conn, **{"app.tasks.ml_risk.score_protocol": {"side_effect": RuntimeError("model broke")}}
            )
        self.assertEqual(written, 1)
        self.assertEqual(risk_methods(conn), ["rule_based"])
        self.assertIn("ML risk scoring failed", "\n".join(logs.output))

    async def test_rule_based_failure_does_not_stop_ml_scoring(self):
        conn = IngestFakeConnection()
        written = await self.run_cycle(
            conn, **{"app.tasks.ingest.score_from_snapshots": {"side_effect": RuntimeError("rules broke")}}
        )
        self.assertEqual(written, 1)
        self.assertEqual(risk_methods(conn), ["ml"])

    async def test_ml_skip_writes_no_ml_rows(self):
        conn = IngestFakeConnection(category="PERPS")
        await self.run_cycle(conn)
        self.assertEqual(risk_methods(conn), ["rule_based"])
        self.assertEqual([q for q, _ in conn.executed if "anomalies" in q], [])

    async def test_escalation_state_reads_only_rule_based_scores(self):
        conn = IngestFakeConnection()
        await self.run_cycle(conn)
        self.assertTrue(conn.fetchrow_queries)
        self.assertIn("method = 'rule_based'", conn.fetchrow_queries[0])

    async def test_ml_inference_runs_off_the_event_loop(self):
        conn = IngestFakeConnection()
        with patch("app.tasks.ml_risk.asyncio.to_thread", wraps=__import__("asyncio").to_thread) as to_thread:
            await self.run_cycle(conn)
        self.assertEqual(to_thread.call_args.args[0], ml_risk.score_protocol)


TEST_DB = os.environ.get("SOLANAGUARD_TEST_DATABASE_URL")


class ConnPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return IngestPool(self.conn).acquire()


@unittest.skipUnless(TEST_DB, "set SOLANAGUARD_TEST_DATABASE_URL to a DISPOSABLE database to run")
class BothDetectorsPersistenceIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Both detectors' outputs coexist on one snapshot, and each reader returns
    only its own result. Runs in a transaction that is always rolled back."""

    async def asyncSetUp(self):
        import asyncpg

        self.conn = await asyncpg.connect(TEST_DB)
        self.tx = self.conn.transaction()
        await self.tx.start()
        self.protocol_id = f"ml-it-{uuid.uuid4().hex[:8]}"
        self.slug = self.protocol_id
        await self.conn.execute(
            "INSERT INTO protocols (id, slug, name, category, \"programId\") VALUES ($1, $2, $3, 'DEX', $4)",
            self.protocol_id, self.slug, "ML integration test", f"prog-{self.protocol_id}",
        )
        self.rows = snapshot_rows(40)
        for row in self.rows:
            row["id"] = f"{self.protocol_id}-{row['id']}"
            await self.conn.execute(
                'INSERT INTO protocol_snapshots (id, "protocolId", "tvlUsd", "txCount24h", "snapshotAt") '
                "VALUES ($1, $2, $3, $4, $5)",
                row["id"], self.protocol_id, row["tvlUsd"], row["txCount24h"], row["snapshotAt"],
            )
        self.latest = self.rows[-1]
        # Rule-based score scored a minute before the ML score that follows.
        await self.conn.execute(
            'INSERT INTO risk_scores (id, "protocolId", "overallScore", "riskLevel", method, explanation, "scoredAt") '
            "VALUES ($1, $2, 15, 'LOW', 'rule_based', 'rule', $3)",
            str(uuid.uuid4()), self.protocol_id, self.latest["snapshotAt"] - timedelta(minutes=1),
        )
        # Time-window detector anomaly on the same snapshot and type.
        await self.conn.execute(
            'INSERT INTO anomalies (id, "protocolId", "sourceSnapshotId", "detectorVersion", "detectionMethod", '
            'type, severity, score, "rawScore", "scoreThreshold", description, "detectedAt") '
            "VALUES ($1, $2, $3, 'iforest-v2-time-window', 'isolation_forest', 'TX_SPIKE', 'HIGH', "
            "99.7, 0.61, 0.55, 'time-window', $4)",
            str(uuid.uuid4()), self.protocol_id, self.latest["id"], self.latest["snapshotAt"],
        )

    async def asyncTearDown(self):
        try:
            await self.tx.rollback()
        finally:
            await self.conn.close()

    async def test_outputs_coexist_and_readers_stay_separate(self):
        from app.routers import anomalies as anomalies_router
        from app.routers import protocols as protocols_router

        with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result("TX_SPIKE")):
            outcome = await ml_risk.assess_ml_risk(self.conn, self.protocol_id, self.slug, self.latest["id"])
        self.assertTrue(outcome.anomaly_written)

        # Re-running for the same snapshot is idempotent for the anomaly row.
        with patch("app.tasks.ml_risk.score_protocol", return_value=ml_result("TX_SPIKE")):
            again = await ml_risk.assess_ml_risk(self.conn, self.protocol_id, self.slug, self.latest["id"])
        self.assertFalse(again.anomaly_written)

        stored = await self.conn.fetch(
            'SELECT "detectorVersion", score FROM anomalies WHERE "sourceSnapshotId" = $1 ORDER BY "detectorVersion"',
            self.latest["id"],
        )
        self.assertEqual(
            [(r["detectorVersion"], r["score"]) for r in stored],
            [("iforest-v2-time-window", 99.7), (ml_risk.ML_DETECTOR_VERSION, 80.0)],
        )

        pool = ConnPool(self.conn)
        with patch("app.routers.anomalies.get_pool", AsyncMock(return_value=pool)):
            feed = await anomalies_router.list_anomalies(limit=50, offset=0, protocol_slug=self.slug, severity=None)
        self.assertEqual(feed.pagination.total, 1)
        self.assertEqual(feed.data[0].detector.version, "iforest-v2-time-window")
        self.assertEqual(feed.data[0].calibrated_score, 99.7)

        with patch("app.routers.protocols.get_pool", AsyncMock(return_value=pool)):
            latest = await protocols_router.get_latest_risk_score(self.slug)
            listed = await protocols_router.list_protocols()
        self.assertEqual((latest["method"], latest["overallScore"]), ("rule_based", 15))
        mine = next(p for p in listed if p["slug"] == self.slug)
        self.assertEqual(mine["overallScore"], 15)

        methods = await self.conn.fetch(
            'SELECT method FROM risk_scores WHERE "protocolId" = $1 ORDER BY "scoredAt"', self.protocol_id
        )
        self.assertEqual([r["method"] for r in methods], ["rule_based", "ml", "ml"])

    async def test_real_engine_end_to_end_on_database_rows(self):
        outcome = await ml_risk.assess_ml_risk(self.conn, self.protocol_id, self.slug, self.latest["id"])
        self.assertEqual(outcome.status, "scored")
        row = await self.conn.fetchrow(
            "SELECT \"riskLevel\"::text AS level, explanation FROM risk_scores "
            "WHERE \"protocolId\" = $1 AND method = 'ml'",
            self.protocol_id,
        )
        self.assertIn(row["level"], {"LOW", "MEDIUM", "HIGH", "CRITICAL"})
        self.assertIn(ml_risk.ML_DETECTOR_VERSION, row["explanation"])

    async def test_unavailable_tx_count_skips_without_writes(self):
        await self.conn.execute(
            'UPDATE protocol_snapshots SET "txCount24h" = NULL WHERE id = $1', self.rows[-5]["id"]
        )
        outcome = await ml_risk.assess_ml_risk(self.conn, self.protocol_id, self.slug, self.latest["id"])
        self.assertEqual(outcome.reason, "tx_count_unavailable")
        count = await self.conn.fetchval(
            "SELECT count(*) FROM risk_scores WHERE \"protocolId\" = $1 AND method = 'ml'", self.protocol_id
        )
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
