"""Conservative, time-aware protocol anomaly detection.

The detector uses only populated snapshot fields: ``tvlUsd``,
``txCount24h``, and ``snapshotAt``. ``txCount24h`` is eligible only after
ingestion has measured a real 24-hour program-address signature window.
TVL is a capital proxy; it is never presented as market liquidity depth.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

import asyncpg
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from app.db import get_pool


logger = logging.getLogger(__name__)

DETECTOR_VERSION = "iforest-v2-time-window"
EXPECTED_CADENCE_SECONDS = 300
MIN_CADENCE_SECONDS = 150
MAX_CADENCE_SECONDS = 450
BASELINE_LOOKBACK = timedelta(hours=24)
MIN_BASELINE_SPAN = timedelta(hours=23)
MIN_BASELINE_POINTS = 240
MIN_TVL_USD = 1.0
MIN_ACTIVITY_STD = 1e-6
TRAIN_WINDOW = timedelta(days=14)
CALIBRATION_WINDOW = timedelta(days=3)
SCORING_WINDOW = timedelta(days=1)
MIN_POINTS_PER_DAY = 240
HISTORY_LOOKBACK_DAYS = 19
N_ESTIMATORS = 300
CALIBRATION_QUANTILE = 0.99


@dataclass(frozen=True)
class Snapshot:
    id: str
    protocol_id: str
    slug: str
    tvl_usd: float
    tx_count_24h: int
    snapshot_at: datetime


@dataclass(frozen=True)
class FeatureRow:
    snapshot: Snapshot
    segment_id: int
    tvl_change_pct: float
    tx_activity_z_score: float
    tx_spike_ratio: float
    tvl_relative_to_baseline: float

    def values(self) -> list[float]:
        return [
            self.tvl_change_pct,
            self.tx_activity_z_score,
            self.tx_spike_ratio,
            self.tvl_relative_to_baseline,
        ]


@dataclass
class FeatureBuildResult:
    rows_by_protocol: dict[str, list[FeatureRow]] = field(default_factory=dict)
    latest_snapshot_by_protocol: dict[str, Snapshot] = field(default_factory=dict)
    latest_segment_by_protocol: dict[str, int] = field(default_factory=dict)
    skips_by_protocol: dict[str, Counter[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DetectedAnomaly:
    feature: FeatureRow
    anomaly_type: str
    severity: str
    score_percentile: float
    raw_score: float
    score_threshold: float
    description: str


@dataclass
class DetectorReport:
    snapshots_processed: int = 0
    feature_rows_built: int = 0
    feature_rows_by_protocol: dict[str, int] = field(default_factory=dict)
    skipped_by_protocol: dict[str, str] = field(default_factory=dict)
    failures_by_protocol: dict[str, str] = field(default_factory=dict)
    model_candidates_detected: int = 0
    explainable_anomalies_detected: int = 0
    anomalies_inserted: int = 0
    duplicate_anomalies_skipped: int = 0
    persistence_failures: int = 0
    anomalies_by_protocol: dict[str, int] = field(default_factory=dict)
    anomalies_by_type: dict[str, int] = field(default_factory=dict)
    anomalies_by_severity: dict[str, int] = field(default_factory=dict)
    score_distribution_by_protocol: dict[str, dict[str, float]] = field(default_factory=dict)
    feature_skips_by_protocol: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtocolDetection:
    anomalies: list[DetectedAnomaly]
    model_candidates: int
    score_distribution: dict[str, float] | None
    reason: str | None


async def fetch_active_protocol_slugs(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT slug FROM protocols WHERE "isActive" = true ORDER BY slug')
    return [row["slug"] for row in rows]


async def fetch_snapshots(pool: asyncpg.Pool) -> list[Snapshot]:
    """Fetch only each protocol's 19-day model horizon, in chronological order."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f'''
            WITH latest AS (
                SELECT s."protocolId", max(s."snapshotAt") AS latest_at
                FROM protocol_snapshots AS s
                JOIN protocols AS p ON p.id = s."protocolId"
                WHERE p."isActive" = true
                GROUP BY s."protocolId"
            )
            SELECT
                s.id,
                s."protocolId",
                p.slug,
                s."tvlUsd",
                s."txCount24h",
                s."snapshotAt"
            FROM protocol_snapshots AS s
            JOIN latest AS l ON l."protocolId" = s."protocolId"
            JOIN protocols AS p ON p.id = s."protocolId"
            WHERE s."snapshotAt" >= l.latest_at - INTERVAL '{HISTORY_LOOKBACK_DAYS} days'
            ORDER BY p.slug, s."snapshotAt", s.id
            '''
        )
    return [
        Snapshot(
            id=row["id"],
            protocol_id=row["protocolId"],
            slug=row["slug"],
            tvl_usd=float(row["tvlUsd"]),
            tx_count_24h=int(row["txCount24h"]),
            snapshot_at=row["snapshotAt"],
        )
        for row in rows
    ]


def _is_valid_snapshot(snapshot: Snapshot) -> bool:
    return (
        math.isfinite(snapshot.tvl_usd)
        and snapshot.tvl_usd >= MIN_TVL_USD
        and snapshot.tx_count_24h >= 0
    )


def _is_legacy_capped_activity(baseline_tx: np.ndarray) -> bool:
    """Reject the observed legacy `{0, 100}` capped-signature source pattern."""
    values = {int(value) for value in baseline_tx}
    return bool(values) and values.issubset({0, 100}) and 100 in values


def build_features(snapshots: Iterable[Snapshot]) -> FeatureBuildResult:
    """Build features from strictly preceding snapshots in one raw segment.

    ``tvl_change_pct = 100 * (T_i - T_{i-1}) / T_{i-1}``.
    ``tx_activity_z_score`` uses ``log1p(txCount24h)`` over prior 24-hour
    activity. ``tx_spike_ratio = (X_i + 1) / (median(X_baseline) + 1)``.
    ``tvl_relative_to_baseline = T_i / median(T_baseline)`` is a TVL-capital
    proxy, not a liquidity-depth measurement.
    """
    snapshots_by_protocol: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        snapshots_by_protocol[snapshot.slug].append(snapshot)

    result = FeatureBuildResult()
    for slug, protocol_rows in snapshots_by_protocol.items():
        protocol_rows.sort(key=lambda row: (row.snapshot_at, row.id))
        rows: list[FeatureRow] = []
        skips: Counter[str] = Counter()
        segment: list[Snapshot] = []
        segment_id = 0
        previous: Snapshot | None = None

        for snapshot in protocol_rows:
            if previous is not None:
                gap_seconds = (snapshot.snapshot_at - previous.snapshot_at).total_seconds()
                if gap_seconds <= 0:
                    skips["non_monotonic_timestamp"] += 1
                    segment = []
                    segment_id += 1
                elif gap_seconds < MIN_CADENCE_SECONDS or gap_seconds > MAX_CADENCE_SECONDS:
                    skips["cadence_gap"] += 1
                    segment = []
                    segment_id += 1

            if not _is_valid_snapshot(snapshot):
                skips["invalid_raw_snapshot"] += 1
                segment = []
                segment_id += 1
                previous = snapshot
                result.latest_snapshot_by_protocol[slug] = snapshot
                result.latest_segment_by_protocol[slug] = segment_id
                continue

            result.latest_snapshot_by_protocol[slug] = snapshot
            result.latest_segment_by_protocol[slug] = segment_id
            baseline_start = snapshot.snapshot_at - BASELINE_LOOKBACK
            segment = [row for row in segment if row.snapshot_at >= baseline_start]
            if not segment or snapshot.snapshot_at - segment[0].snapshot_at < MIN_BASELINE_SPAN:
                skips["insufficient_time_baseline"] += 1
                segment.append(snapshot)
                previous = snapshot
                continue

            if len(segment) < MIN_BASELINE_POINTS:
                skips["insufficient_baseline_coverage"] += 1
                segment.append(snapshot)
                previous = snapshot
                continue

            baseline_tx = np.asarray([row.tx_count_24h for row in segment], dtype=float)
            if _is_legacy_capped_activity(baseline_tx):
                skips["legacy_capped_activity_baseline"] += 1
                segment.append(snapshot)
                previous = snapshot
                continue

            baseline_activity = np.log1p(baseline_tx)
            activity_std = float(np.std(baseline_activity, ddof=1))
            if not math.isfinite(activity_std) or activity_std < MIN_ACTIVITY_STD:
                skips["unstable_activity_baseline"] += 1
                segment.append(snapshot)
                previous = snapshot
                continue

            baseline_tvl = np.asarray([row.tvl_usd for row in segment], dtype=float)
            previous_tvl = segment[-1].tvl_usd
            values = np.asarray(
                [
                    100.0 * (snapshot.tvl_usd - previous_tvl) / previous_tvl,
                    (math.log1p(snapshot.tx_count_24h) - float(np.mean(baseline_activity))) / activity_std,
                    (snapshot.tx_count_24h + 1.0) / (float(np.median(baseline_tx)) + 1.0),
                    snapshot.tvl_usd / float(np.median(baseline_tvl)),
                ],
                dtype=float,
            )
            if not np.isfinite(values).all():
                skips["non_finite_features"] += 1
                segment.append(snapshot)
                previous = snapshot
                continue

            rows.append(
                FeatureRow(
                    snapshot=snapshot,
                    segment_id=segment_id,
                    tvl_change_pct=float(values[0]),
                    tx_activity_z_score=float(values[1]),
                    tx_spike_ratio=float(values[2]),
                    tvl_relative_to_baseline=float(values[3]),
                )
            )
            segment.append(snapshot)
            previous = snapshot

        result.rows_by_protocol[slug] = rows
        result.skips_by_protocol[slug] = skips
    return result


def _classify_explainable_anomaly(feature: FeatureRow) -> str | None:
    if feature.tvl_change_pct <= -5.0 and feature.tvl_relative_to_baseline <= 0.95:
        return "TVL_DECLINE"
    if feature.tx_spike_ratio >= 2.0 and feature.tx_activity_z_score >= 2.0:
        return "TX_SPIKE"
    return None


def _severity(score_percentile: float) -> str:
    if score_percentile >= 99.9:
        return "CRITICAL"
    if score_percentile >= 99.5:
        return "HIGH"
    return "MEDIUM"


def _description(
    feature: FeatureRow,
    anomaly_type: str,
    score_percentile: float,
    raw_score: float,
    score_threshold: float,
) -> str:
    if anomaly_type == "TVL_DECLINE":
        evidence = (
            f"TVL changed {feature.tvl_change_pct:.2f}% from the prior snapshot; "
            f"TVL is {feature.tvl_relative_to_baseline:.3f}x its trailing 24-hour median"
        )
    else:
        evidence = (
            f"24-hour transaction count is {feature.tx_spike_ratio:.2f}x its trailing median "
            f"with activity z-score {feature.tx_activity_z_score:.2f}"
        )
    return (
        f"Isolation Forest score {raw_score:.6f} exceeded the held-out P99 threshold "
        f"{score_threshold:.6f} ({score_percentile:.2f}th calibration percentile): "
        f"{evidence}. source_snapshot_id={feature.snapshot.id}; detector={DETECTOR_VERSION}"
    )


def detect_for_protocol(
    rows: list[FeatureRow],
    latest_snapshot: Snapshot,
    latest_segment_id: int,
) -> ProtocolDetection:
    """Train, calibrate, and score only the latest raw contiguous segment."""
    current_segment = [row for row in rows if row.segment_id == latest_segment_id]
    if not current_segment:
        return ProtocolDetection([], 0, None, "latest raw segment has no scoreable feature rows")
    if current_segment[-1].snapshot.id != latest_snapshot.id:
        return ProtocolDetection([], 0, None, "latest raw snapshot is not feature-eligible")

    latest_at = latest_snapshot.snapshot_at
    scoring_start = latest_at - SCORING_WINDOW
    calibration_start = scoring_start - CALIBRATION_WINDOW
    training_start = calibration_start - TRAIN_WINDOW
    training = [row for row in current_segment if training_start < row.snapshot.snapshot_at <= calibration_start]
    calibration = [row for row in current_segment if calibration_start < row.snapshot.snapshot_at <= scoring_start]
    scoring = [row for row in current_segment if scoring_start < row.snapshot.snapshot_at <= latest_at]
    required_counts = {
        "training": MIN_POINTS_PER_DAY * TRAIN_WINDOW.days,
        "calibration": MIN_POINTS_PER_DAY * CALIBRATION_WINDOW.days,
        "scoring": MIN_POINTS_PER_DAY * SCORING_WINDOW.days,
    }
    observed_counts = {
        "training": len(training),
        "calibration": len(calibration),
        "scoring": len(scoring),
    }
    if any(observed_counts[name] < required_counts[name] for name in required_counts):
        return ProtocolDetection(
            [],
            0,
            None,
            "insufficient time-partitioned history "
            f"(training={observed_counts['training']}/{required_counts['training']}, "
            f"calibration={observed_counts['calibration']}/{required_counts['calibration']}, "
            f"scoring={observed_counts['scoring']}/{required_counts['scoring']})",
        )

    training_values = np.asarray([row.values() for row in training], dtype=float)
    calibration_values = np.asarray([row.values() for row in calibration], dtype=float)
    scoring_values = np.asarray([row.values() for row in scoring], dtype=float)
    if not (
        np.isfinite(training_values).all()
        and np.isfinite(calibration_values).all()
        and np.isfinite(scoring_values).all()
    ):
        return ProtocolDetection([], 0, None, "non-finite model input")

    scaler = RobustScaler()
    training_scaled = scaler.fit_transform(training_values)
    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        max_samples=256,
        max_features=1.0,
        bootstrap=False,
        # score_samples is calibrated on held-out data; sklearn's offset is not used.
        contamination="auto",
        random_state=42,
        n_jobs=1,
    )
    model.fit(training_scaled)
    calibration_scores = -model.score_samples(scaler.transform(calibration_values))
    score_threshold = float(
        np.quantile(calibration_scores, CALIBRATION_QUANTILE, method="higher")
    )
    scoring_scores = -model.score_samples(scaler.transform(scoring_values))
    model_candidate_count = int(np.count_nonzero(scoring_scores > score_threshold))

    anomalies: list[DetectedAnomaly] = []
    for feature, raw_score in zip(scoring, scoring_scores, strict=True):
        if raw_score <= score_threshold:
            continue
        anomaly_type = _classify_explainable_anomaly(feature)
        if anomaly_type is None:
            continue
        score_percentile = float(100.0 * np.mean(calibration_scores < raw_score))
        anomalies.append(
            DetectedAnomaly(
                feature=feature,
                anomaly_type=anomaly_type,
                severity=_severity(score_percentile),
                score_percentile=score_percentile,
                raw_score=float(raw_score),
                score_threshold=score_threshold,
                description=_description(
                    feature,
                    anomaly_type,
                    score_percentile,
                    float(raw_score),
                    score_threshold,
                ),
            )
        )

    return ProtocolDetection(
        anomalies=anomalies,
        model_candidates=model_candidate_count,
        score_distribution={
            "calibration_p50": float(np.quantile(calibration_scores, 0.50)),
            "calibration_p95": float(np.quantile(calibration_scores, 0.95)),
            "calibration_p99": score_threshold,
            "scoring_min": float(np.min(scoring_scores)),
            "scoring_p50": float(np.quantile(scoring_scores, 0.50)),
            "scoring_max": float(np.max(scoring_scores)),
            "training_rows": float(len(training)),
            "calibration_rows": float(len(calibration)),
            "scoring_rows": float(len(scoring)),
        },
        reason=None,
    )


async def persist_anomalies(pool: asyncpg.Pool, anomalies: Iterable[DetectedAnomaly]) -> tuple[int, int, int]:
    """Persist source-snapshot identities with database-enforced idempotency."""
    inserted = 0
    duplicates = 0
    failures = 0
    async with pool.acquire() as conn:
        for anomaly in anomalies:
            snapshot = anomaly.feature.snapshot
            try:
                async with conn.transaction():
                    inserted_id = await conn.fetchval(
                        '''
                        INSERT INTO anomalies (
                            id, "protocolId", "sourceSnapshotId", "detectorVersion", "detectionMethod",
                            type, severity, score, "rawScore", "scoreThreshold", description, "detectedAt"
                        )
                        VALUES ($1, $2, $3, $4, $5, $6::"AnomalyType", $7::"RiskLevel", $8, $9, $10, $11, $12)
                        ON CONFLICT ("protocolId", "sourceSnapshotId", "detectorVersion", type) DO NOTHING
                        RETURNING id
                        ''',
                        str(uuid.uuid4()),
                        snapshot.protocol_id,
                        snapshot.id,
                        DETECTOR_VERSION,
                        "isolation_forest",
                        anomaly.anomaly_type,
                        anomaly.severity,
                        anomaly.score_percentile,
                        anomaly.raw_score,
                        anomaly.score_threshold,
                        anomaly.description,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                    )
            except Exception:
                failures += 1
                logger.exception(
                    "Failed to persist anomaly for protocol=%s snapshot=%s",
                    snapshot.slug,
                    snapshot.id,
                )
                continue
            if inserted_id is None:
                duplicates += 1
            else:
                inserted += 1
    return inserted, duplicates, failures


async def run_detector() -> DetectorReport:
    """Run the detector; unhealthy protocols abstain without blocking others."""
    pool = await get_pool()
    active_protocols = await fetch_active_protocol_slugs(pool)
    snapshots = await fetch_snapshots(pool)
    report = DetectorReport(snapshots_processed=len(snapshots))
    build_result = build_features(snapshots)
    report.feature_rows_built = sum(len(rows) for rows in build_result.rows_by_protocol.values())
    report.feature_skips_by_protocol = {
        slug: dict(skips) for slug, skips in build_result.skips_by_protocol.items()
    }

    all_anomalies: list[DetectedAnomaly] = []
    for slug in active_protocols:
        rows = build_result.rows_by_protocol.get(slug, [])
        report.feature_rows_by_protocol[slug] = len(rows)
        latest_snapshot = build_result.latest_snapshot_by_protocol.get(slug)
        latest_segment_id = build_result.latest_segment_by_protocol.get(slug)
        if latest_snapshot is None or latest_segment_id is None:
            report.skipped_by_protocol[slug] = "no snapshots in the model horizon"
            continue
        try:
            outcome = detect_for_protocol(rows, latest_snapshot, latest_segment_id)
        except Exception as error:
            logger.exception("Detector failed for protocol=%s", slug)
            report.failures_by_protocol[slug] = f"{type(error).__name__}: {error}"
            continue
        if outcome.reason is not None:
            report.skipped_by_protocol[slug] = outcome.reason
            continue
        report.model_candidates_detected += outcome.model_candidates
        report.explainable_anomalies_detected += len(outcome.anomalies)
        report.score_distribution_by_protocol[slug] = outcome.score_distribution or {}
        all_anomalies.extend(outcome.anomalies)

    inserted, duplicates, failures = await persist_anomalies(pool, all_anomalies)
    report.anomalies_inserted = inserted
    report.duplicate_anomalies_skipped = duplicates
    report.persistence_failures = failures
    report.anomalies_by_protocol = dict(Counter(a.feature.snapshot.slug for a in all_anomalies))
    report.anomalies_by_type = dict(Counter(a.anomaly_type for a in all_anomalies))
    report.anomalies_by_severity = dict(Counter(a.severity for a in all_anomalies))
    return report
