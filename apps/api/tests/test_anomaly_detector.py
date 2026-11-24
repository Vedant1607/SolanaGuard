from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.ml.anomaly_detector import (
    FeatureRow,
    Snapshot,
    _classify_explainable_anomaly,
    build_features,
    detect_for_protocol,
)
from app.solana.client import SolanaClient


def snapshots(count: int, *, start: datetime | None = None) -> list[Snapshot]:
    base = start or datetime(2026, 1, 1)
    return [
        Snapshot(
            id=str(index),
            protocol_id="protocol-id",
            slug="protocol",
            tvl_usd=1_000_000.0,
            tx_count_24h=1_000 + index % 7,
            snapshot_at=base + timedelta(minutes=5 * index),
        )
        for index in range(count)
    ]


class FeatureBuilderTests(unittest.TestCase):
    def test_current_snapshot_is_excluded_from_its_baseline(self) -> None:
        rows = snapshots(300)
        current = rows[-1]
        rows[-1] = Snapshot(
            id=current.id,
            protocol_id=current.protocol_id,
            slug=current.slug,
            tvl_usd=2_000_000.0,
            tx_count_24h=current.tx_count_24h,
            snapshot_at=current.snapshot_at,
        )

        result = build_features(reversed(rows))
        feature = result.rows_by_protocol["protocol"][-1]

        self.assertAlmostEqual(feature.tvl_change_pct, 100.0)
        self.assertAlmostEqual(feature.tvl_relative_to_baseline, 2.0)

    def test_legacy_binary_activity_is_not_modelled(self) -> None:
        rows = snapshots(300)
        rows = [
            Snapshot(
                id=row.id,
                protocol_id=row.protocol_id,
                slug=row.slug,
                tvl_usd=row.tvl_usd,
                tx_count_24h=100 if int(row.id) % 2 else 0,
                snapshot_at=row.snapshot_at,
            )
            for row in rows
        ]

        result = build_features(rows)

        self.assertEqual(result.rows_by_protocol["protocol"], [])
        self.assertGreater(result.skips_by_protocol["protocol"]["legacy_capped_activity_baseline"], 0)

    def test_latest_short_segment_cannot_score_older_history(self) -> None:
        rows = snapshots(5_500)
        old_latest = rows[-1]
        rows.append(
            Snapshot(
                id="new-segment",
                protocol_id=old_latest.protocol_id,
                slug=old_latest.slug,
                tvl_usd=old_latest.tvl_usd,
                tx_count_24h=old_latest.tx_count_24h,
                snapshot_at=old_latest.snapshot_at + timedelta(hours=2),
            )
        )

        result = build_features(rows)
        outcome = detect_for_protocol(
            result.rows_by_protocol["protocol"],
            result.latest_snapshot_by_protocol["protocol"],
            result.latest_segment_by_protocol["protocol"],
        )

        self.assertEqual(outcome.reason, "latest raw segment has no scoreable feature rows")

    def test_tvl_signal_is_not_classified_as_liquidity_withdrawal(self) -> None:
        row = snapshots(1)[0]
        feature = FeatureRow(
            snapshot=row,
            segment_id=0,
            tvl_change_pct=-10.0,
            tx_activity_z_score=0.0,
            tx_spike_ratio=1.0,
            tvl_relative_to_baseline=0.9,
        )

        self.assertEqual(_classify_explainable_anomaly(feature), "TVL_DECLINE")


class SignatureWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_count_signatures_since_paginates_to_time_boundary(self) -> None:
        client = SolanaClient()
        pages = [
            [
                {"signature": "newest", "blockTime": 120},
                {"signature": "boundary", "blockTime": 100},
            ],
            [{"signature": "older", "blockTime": 99}],
        ]

        async def fake_rpc(method: str, params: list) -> list[dict[str, int | str]]:
            self.assertEqual(method, "getSignaturesForAddress")
            return pages.pop(0)

        client._rpc = fake_rpc  # type: ignore[method-assign]

        self.assertEqual(await client.count_signatures_since("program", 100), 2)

    async def test_count_signatures_since_rejects_missing_block_time(self) -> None:
        client = SolanaClient()

        async def fake_rpc(method: str, params: list) -> list[dict[str, str]]:
            return [{"signature": "unknown"}]

        client._rpc = fake_rpc  # type: ignore[method-assign]

        with self.assertRaises(ValueError):
            await client.count_signatures_since("program", 100)
