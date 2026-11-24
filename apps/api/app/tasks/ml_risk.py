"""Ingestion step for the category ML risk engine (PR #1), kept independent.

SolanaGuard runs two independent anomaly systems:

* ``app.ml.anomaly_detector`` — the time-window detector (``iforest-v2-time-window``).
  It runs on its own schedule, under its own advisory lock, and persists
  calibration-percentile anomalies that the ``/api/v1/anomalies`` feed serves.
* ``app.ml.engine`` — this step. After each fresh snapshot it scores one
  protocol with a per-category Isolation Forest and a weighted risk composite.

Their scores are different quantities and are never combined. This step:

* writes its risk score to ``risk_scores`` with ``method = 'ml'``. Readers of
  the displayed, alerting score select ``method = 'rule_based'``, so an ML row
  never replaces the rule-based score and never triggers escalation alerts;
* writes its anomaly, when flagged, to ``anomalies`` under its own
  ``detectorVersion``/``detectionMethod``. The unique key
  ``(protocolId, sourceSnapshotId, detectorVersion, type)`` keeps it apart
  from the other detector's rows, and the feed excludes it because its score
  is not a calibration percentile;
* writes nothing when it cannot score: insufficient history, unavailable
  transaction counts, a history gap, an unsupported category, or a failure.
  The skip is reported, never recorded as a NORMAL/LOW result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from app.ml.engine import MODEL_FILES, score_protocol

logger = logging.getLogger(__name__)

ML_RISK_METHOD = "ml"  # risk_scores.method value used by PR #1
ML_DETECTOR_VERSION = "ml-risk-category-iforest-v1"
ML_DETECTION_METHOD = "category_isolation_forest_minmax"
ML_ANOMALY_SCORE_THRESHOLD = 55.0  # category_anomaly_detector.ANOMALY_SCORE_THRESHOLD (0-100 scale)

# Most recent snapshots passed to the engine. Its 0-100 anomaly score is a
# min-max rescaling within the scored batch, so the batch must be bounded to
# keep per-cycle cost constant (about 7 days at the 5-minute cadence).
ML_RISK_HISTORY_LIMIT = 2016
# The engine's rolling features assume consecutive rows are evenly spaced.
# A gap inside the last rolling window plus the scored row makes them
# meaningless, so that case is skipped.
ML_FEATURE_ROWS = 25  # ROLLING_WINDOW (24) + the scored row
ML_MAX_SNAPSHOT_GAP = timedelta(seconds=450)  # anomaly_detector.MAX_CADENCE_SECONDS

# Inputs each anomaly type's classification depends on. Ingestion does not
# collect volume, unique wallets, or liquidity depth (they default to 0), so
# a type that depends on them would describe something that was never measured.
_TYPE_REQUIRED_INPUTS = {
    "TX_SPIKE": ("txCount24h",),
    "LIQUIDITY_WITHDRAWAL": ("liquidityDepth",),
    "WHALE_MOVEMENT": ("uniqueWallets24h",),
    "PRICE_DEVIATION": ("volume24hUsd", "liquidityDepth"),
}
_OPTIONAL_INPUTS = ("volume24hUsd", "uniqueWallets24h", "liquidityDepth", "utilizationRate")

ML_SNAPSHOT_QUERY = '''
    SELECT id, "tvlUsd", "volume24hUsd", "txCount24h", "uniqueWallets24h",
           "liquidityDepth", "utilizationRate", "snapshotAt"
    FROM protocol_snapshots
    WHERE "protocolId" = $1
    ORDER BY "snapshotAt" DESC, id DESC
    LIMIT $2
'''


@dataclass
class MlRiskOutcome:
    status: str  # "scored" | "skipped" | "failed"
    reason: str | None = None
    risk_score_written: bool = False
    anomaly_written: bool = False
    anomaly_not_persisted_reason: str | None = None
    unmeasured_inputs: list[str] = field(default_factory=list)


def _unmeasured_inputs(snapshots: list[dict]) -> list[str]:
    return [name for name in _OPTIONAL_INPUTS if not any(row[name] for row in snapshots)]


def _has_gap(snapshots: list[dict]) -> bool:
    tail = snapshots[-ML_FEATURE_ROWS:]
    return any(
        later["snapshotAt"] - earlier["snapshotAt"] > ML_MAX_SNAPSHOT_GAP
        for earlier, later in zip(tail, tail[1:])
    )


def _explanation(ml_result: dict, unmeasured: list[str]) -> str:
    return (
        f"ML risk score ({ML_DETECTOR_VERSION}; weighted composite incl. placeholder "
        f"smart-contract/governance inputs, confidence {ml_result.get('confidence')}). "
        f"Unmeasured inputs (stored as 0): {','.join(unmeasured) or 'none'}. "
        f"Components: {json.dumps(ml_result.get('components', {}), separators=(',', ':'))}"
    )


async def assess_ml_risk(conn, protocol_id: str, slug: str, snapshot_id: str) -> MlRiskOutcome:
    """Score the snapshot just written with the category ML engine and persist the result."""
    category = await conn.fetchval('SELECT category::text FROM protocols WHERE id = $1', protocol_id)
    if category not in MODEL_FILES:
        return MlRiskOutcome("skipped", "unsupported_category")

    rows = await conn.fetch(ML_SNAPSHOT_QUERY, protocol_id, ML_RISK_HISTORY_LIMIT)
    snapshots = [dict(row) for row in reversed(rows)]
    if not snapshots or snapshots[-1]["id"] != snapshot_id:
        return MlRiskOutcome("skipped", "stale_snapshot")
    if len(snapshots) < ML_FEATURE_ROWS:
        return MlRiskOutcome("skipped", "insufficient_history")
    if any(row["txCount24h"] is None for row in snapshots):
        # The engine would fill missing counts with 0, which reads as "no activity".
        return MlRiskOutcome("skipped", "tx_count_unavailable")
    if _has_gap(snapshots):
        return MlRiskOutcome("skipped", "history_gap")

    unmeasured = _unmeasured_inputs(snapshots)
    engine_rows = [{key: value for key, value in row.items() if key != "id"} for row in snapshots]
    # pandas/scikit-learn are CPU-bound; keep them off the event loop.
    ml_result = await asyncio.to_thread(score_protocol, category, slug, engine_rows)
    if ml_result is None:
        return MlRiskOutcome("skipped", "insufficient_features")

    outcome = MlRiskOutcome("scored", unmeasured_inputs=unmeasured)
    scored_at = snapshots[-1]["snapshotAt"]
    anomaly = ml_result.get("anomaly") or {}
    anomaly_type = anomaly.get("type")
    persist_anomaly = False
    if anomaly.get("is_anomaly"):
        missing = [name for name in _TYPE_REQUIRED_INPUTS.get(anomaly_type, ("unknown_type",))
                   if name in unmeasured or name == "unknown_type"]
        if missing:
            outcome.anomaly_not_persisted_reason = f"unmeasured_inputs:{','.join(missing)}"
        else:
            persist_anomaly = True

    async with conn.transaction():
        await conn.execute(
            '''
            INSERT INTO risk_scores
                (id, "protocolId", "overallScore", "riskLevel", method, explanation, "scoredAt")
            VALUES ($1, $2, $3, $4::"RiskLevel", $5, $6, $7)
            ''',
            str(uuid.uuid4()), protocol_id, ml_result["overall_score"], ml_result["risk_level"],
            ML_RISK_METHOD, _explanation(ml_result, unmeasured), scored_at,
        )
        outcome.risk_score_written = True
        if persist_anomaly:
            status = await conn.execute(
                '''
                INSERT INTO anomalies (
                    id, "protocolId", "sourceSnapshotId", "detectorVersion", "detectionMethod",
                    type, severity, score, "rawScore", "scoreThreshold", description, "detectedAt"
                )
                VALUES ($1, $2, $3, $4, $5, $6::"AnomalyType", $7::"RiskLevel", $8, NULL, $9, $10, $11)
                ON CONFLICT ("protocolId", "sourceSnapshotId", "detectorVersion", type) DO NOTHING
                ''',
                str(uuid.uuid4()), protocol_id, snapshot_id, ML_DETECTOR_VERSION, ML_DETECTION_METHOD,
                anomaly_type, anomaly["severity"], anomaly["score"], ML_ANOMALY_SCORE_THRESHOLD,
                anomaly["description"], scored_at,
            )
            outcome.anomaly_written = status == "INSERT 0 1"

    logger.info(
        "ML risk score: %s = %s (%s) [%s]; anomaly=%s%s",
        slug, ml_result["overall_score"], ml_result["risk_level"], ML_DETECTOR_VERSION,
        anomaly_type if anomaly.get("is_anomaly") else "none",
        f" (not persisted: {outcome.anomaly_not_persisted_reason})" if outcome.anomaly_not_persisted_reason else "",
    )
    return outcome
