"""Nullable txCount24h: NULL means unavailable and is never treated as zero."""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.ml.anomaly_detector import Snapshot, build_features, detect_for_protocol
from app.scoring.rule_based import score_from_snapshots


DATABASE_PACKAGE = Path(__file__).resolve().parents[3] / "packages" / "database"
MIGRATION = DATABASE_PACKAGE / "prisma" / "migrations" / "20261111120000_nullable_tx_count" / "migration.sql"


class RuleBasedScoringTests(unittest.TestCase):
    def test_both_metrics_present_behaviour_unchanged(self) -> None:
        result = score_from_snapshots(
            latest_tvl=750.0, previous_tvl=1_000.0, latest_tx=500, previous_tx=100
        )
        self.assertEqual(result.overall_score, 100.0)  # 15 + 55 (TVL -25%) + 30 (5x tx)
        self.assertEqual(result.risk_level, "CRITICAL")
        self.assertEqual(
            result.explanation,
            "TVL dropped 25.0% since last snapshot; Transaction count 5.0x above last snapshot",
        )

    def test_no_change_with_both_metrics_unchanged(self) -> None:
        result = score_from_snapshots(1_000.0, 1_000.0, 100, 100)
        self.assertEqual((result.overall_score, result.risk_level), (15.0, "LOW"))
        self.assertEqual(result.explanation, "No significant change since last snapshot")

    def test_missing_latest_tx_scores_on_tvl_only(self) -> None:
        result = score_from_snapshots(750.0, 1_000.0, None, 100)
        self.assertEqual(result.overall_score, 70.0)  # 15 + 55; no tx contribution
        self.assertEqual(result.risk_level, "HIGH")
        self.assertIn("TVL dropped 25.0%", result.explanation)
        self.assertIn("transaction count unavailable; scored on TVL only", result.explanation)
        self.assertNotIn("Transaction count", result.explanation)

    def test_missing_previous_tx_is_not_treated_as_zero(self) -> None:
        # With a genuine previous zero the ratio would fall back to 1.0; with
        # NULL the tx rule must simply not run.
        result = score_from_snapshots(1_000.0, 1_000.0, 10_000, None)
        self.assertEqual((result.overall_score, result.risk_level), (15.0, "LOW"))
        self.assertIn("transaction count unavailable", result.explanation)


def snapshot_series(count: int, *, missing_at: set[int] = frozenset()) -> list[Snapshot]:
    base = datetime(2026, 1, 1)
    return [
        Snapshot(
            id=str(index),
            protocol_id="protocol-id",
            slug="protocol",
            tvl_usd=1_000_000.0,
            tx_count_24h=None if index in missing_at else 1_000 + index % 7,
            snapshot_at=base + timedelta(minutes=5 * index),
        )
        for index in range(count)
    ]


class DetectorMissingTxTests(unittest.TestCase):
    def test_null_tx_snapshot_is_rejected_and_never_featurised(self) -> None:
        rows = snapshot_series(300, missing_at={299})
        result = build_features(rows)

        self.assertEqual(result.skips_by_protocol["protocol"]["missing_tx_count"], 1)
        self.assertNotIn("invalid_raw_snapshot", result.skips_by_protocol["protocol"])
        featurised_ids = {row.snapshot.id for row in result.rows_by_protocol["protocol"]}
        self.assertNotIn("299", featurised_ids)

    def test_latest_null_tx_snapshot_makes_detector_abstain(self) -> None:
        rows = snapshot_series(300, missing_at={299})
        result = build_features(rows)
        outcome = detect_for_protocol(
            result.rows_by_protocol["protocol"],
            result.latest_snapshot_by_protocol["protocol"],
            result.latest_segment_by_protocol["protocol"],
        )
        self.assertEqual(outcome.anomalies, [])
        self.assertIsNotNone(outcome.reason)

    def test_null_tx_breaks_baseline_segment(self) -> None:
        # A gap in transaction data must not be bridged: rows after the NULL
        # start a fresh segment and need a full new baseline.
        complete = build_features(snapshot_series(600))
        with_gap = build_features(snapshot_series(600, missing_at={300}))

        self.assertLess(
            len(with_gap.rows_by_protocol["protocol"]),
            len(complete.rows_by_protocol["protocol"]),
        )
        segments_after_gap = {
            row.segment_id
            for row in with_gap.rows_by_protocol["protocol"]
            if int(row.snapshot.id) > 300
        }
        self.assertTrue(all(segment > 0 for segment in segments_after_gap))

    def test_complete_series_unaffected(self) -> None:
        result = build_features(snapshot_series(300))
        self.assertNotIn("missing_tx_count", result.skips_by_protocol["protocol"])
        self.assertTrue(result.rows_by_protocol["protocol"])


@unittest.skipUnless(DATABASE_PACKAGE.is_dir(), "packages/database not available (e.g. inside API container)")
class NullableSchemaTests(unittest.TestCase):
    def test_prisma_schema_declares_optional_tx_count_without_default(self) -> None:
        schema = (DATABASE_PACKAGE / "prisma" / "schema.prisma").read_text()
        line = next(l for l in schema.splitlines() if l.strip().startswith("txCount24h"))
        self.assertRegex(line, r"^\s*txCount24h\s+Int\?")
        self.assertNotIn("@default", line)

    def test_migration_is_schema_only(self) -> None:
        sql = MIGRATION.read_text()
        statements = re.sub(r"--[^\n]*", "", sql)
        self.assertIn('ALTER COLUMN "txCount24h" DROP NOT NULL', statements)
        self.assertIn('ALTER COLUMN "txCount24h" DROP DEFAULT', statements)
        for forbidden in ("UPDATE", "DELETE", "INSERT", "TRUNCATE", "DROP TABLE", "DROP COLUMN"):
            self.assertNotIn(forbidden, statements.upper())


if __name__ == "__main__":
    unittest.main()
