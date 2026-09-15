import asyncio
import uuid
import logging
import json
from datetime import datetime, timezone

from app.db import get_pool
from app.solana.protocols import LAUNCH_PROTOCOLS, get_protocol_metrics
from app.scoring.rule_based import score_from_snapshots
from app.ml.engine import score_protocol
from app.alerts.email import send_alert_email
from app.alerts.telegram import send_telegram_message

logger = logging.getLogger(__name__)

_ingestion_lock = asyncio.Lock()


def _risk_explanation(ml_result: dict) -> str:
    """Create a compact database-friendly explanation for an ML score."""
    components = ml_result.get("components", {})
    return (
        f"ML risk score generated from anomaly and protocol risk signals. "
        f"Components: {json.dumps(components, separators=(',', ':'))}"
    )


async def ingest_all_protocols() -> int:
    """
    Fetch live metrics for every launch protocol, write a snapshot,
    and score the protocol.

    ML scoring is used once at least 25 snapshots are available.
    Until then, the existing rule-based scorer is used as a fallback.
    """
    if _ingestion_lock.locked():
        logger.info("Ingestion already in progress, skipping this trigger")
        return 0

    async with _ingestion_lock:
        pool = await get_pool()
        written = 0

        for slug in LAUNCH_PROTOCOLS:
            try:
                metrics = await get_protocol_metrics(slug)

                if metrics is None:
                    logger.warning(f"No metrics returned for {slug}, skipping")
                    continue

                async with pool.acquire() as conn:
                    protocol_row = await conn.fetchrow(
                        'SELECT id, "category" FROM protocols WHERE slug = $1',
                        slug,
                    )

                    if protocol_row is None:
                        logger.warning(
                            f"No protocol row for slug={slug}, skipping"
                        )
                        continue

                    protocol_id = protocol_row["id"]
                    category = protocol_row["category"]

                    snapshot_at = datetime.now(timezone.utc).replace(tzinfo=None)

                    await conn.execute(
                        '''
                        INSERT INTO protocol_snapshots
                            (
                                id,
                                "protocolId",
                                "tvlUsd",
                                "txCount24h",
                                "snapshotAt"
                            )
                        VALUES ($1, $2, $3, $4, $5)
                        ''',
                        str(uuid.uuid4()),
                        protocol_id,
                        metrics.tvl_usd,
                        metrics.tx_count_24h,
                        snapshot_at,
                    )

                    written += 1

                    logger.info(
                        f"Snapshot written: {slug} "
                        f"TVL=${metrics.tvl_usd:,.0f} "
                        f"tx24h={metrics.tx_count_24h}"
                    )

                    # Fetch enough historical data for the ML feature pipeline.
                    snapshot_rows = await conn.fetch(
                        '''
                        SELECT
                            "tvlUsd",
                            "volume24hUsd",
                            "txCount24h",
                            "uniqueWallets24h",
                            "liquidityDepth",
                            "utilizationRate",
                            "snapshotAt"
                        FROM protocol_snapshots
                        WHERE "protocolId" = $1
                        ORDER BY "snapshotAt" ASC
                        ''',
                        protocol_id,
                    )

                    ml_result = None

                    if len(snapshot_rows) > 24:
                        snapshots = [
                            {
                                "tvlUsd": row["tvlUsd"],
                                "volume24hUsd": row["volume24hUsd"],
                                "txCount24h": row["txCount24h"],
                                "uniqueWallets24h": row["uniqueWallets24h"],
                                "liquidityDepth": row["liquidityDepth"],
                                "utilizationRate": row["utilizationRate"],
                                "snapshotAt": row["snapshotAt"],
                            }
                            for row in snapshot_rows
                        ]

                        try:
                            ml_result = score_protocol(
                                category=category,
                                protocol_slug=slug,
                                snapshots=snapshots,
                            )
                        except Exception:
                            logger.exception(
                                f"ML scoring failed for {slug}; "
                                f"falling back to rule-based scoring"
                            )

                    if ml_result is not None:
                        overall_score = ml_result["overall_score"]
                        risk_level = ml_result["risk_level"]
                        method = "ml"
                        explanation = _risk_explanation(ml_result)

                        anomaly = ml_result.get("anomaly")

                        if anomaly and anomaly.get("is_anomaly"):
                            anomaly_type = anomaly.get("type")
                            severity = anomaly.get("severity")

                            valid_types = {
                                "LIQUIDITY_WITHDRAWAL",
                                "TX_SPIKE",
                                "WHALE_MOVEMENT",
                                "PRICE_DEVIATION",
                                "RUG_PULL_SIGNAL",
                            }

                            valid_levels = {
                                "LOW",
                                "MEDIUM",
                                "HIGH",
                                "CRITICAL",
                            }

                            if (
                                anomaly_type in valid_types
                                and severity in valid_levels
                            ):
                                await conn.execute(
                                    '''
                                    INSERT INTO anomalies
                                        (
                                            id,
                                            "protocolId",
                                            type,
                                            severity,
                                            score,
                                            description,
                                            "detectedAt"
                                        )
                                    VALUES
                                        (
                                            $1,
                                            $2,
                                            $3::"AnomalyType",
                                            $4::"RiskLevel",
                                            $5,
                                            $6,
                                            $7
                                        )
                                    ''',
                                    str(uuid.uuid4()),
                                    protocol_id,
                                    anomaly_type,
                                    severity,
                                    anomaly["score"],
                                    anomaly.get(
                                        "description",
                                        "ML anomaly detected",
                                    ),
                                    snapshot_at,
                                )

                                logger.warning(
                                    f"ML anomaly: {slug} "
                                    f"type={anomaly_type} "
                                    f"severity={severity} "
                                    f"score={anomaly['score']}"
                                )

                    else:
                        # Keep the existing rule-based scorer active until
                        # enough ML history is available.
                        if len(snapshot_rows) >= 2:
                            latest = snapshot_rows[-1]
                            previous = snapshot_rows[-2]

                            result = score_from_snapshots(
                                latest_tvl=latest["tvlUsd"],
                                previous_tvl=previous["tvlUsd"],
                                latest_tx=latest["txCount24h"],
                                previous_tx=previous["txCount24h"],
                            )

                            overall_score = result.overall_score
                            risk_level = result.risk_level
                            method = "rule_based"
                            explanation = result.explanation
                        else:
                            logger.info(
                                f"Not enough history yet for {slug} — "
                                f"skipping score"
                            )
                            continue

                    previous_level_row = await conn.fetchrow(
                        '''
                        SELECT "riskLevel"
                        FROM risk_scores
                        WHERE "protocolId" = $1
                        ORDER BY "scoredAt" DESC
                        LIMIT 1
                        ''',
                        protocol_id,
                    )

                    previous_level = (
                        previous_level_row["riskLevel"]
                        if previous_level_row
                        else None
                    )

                    escalated = (
                        risk_level in ("HIGH", "CRITICAL")
                        and risk_level != previous_level
                    )

                    await conn.execute(
                        '''
                        INSERT INTO risk_scores
                            (
                                id,
                                "protocolId",
                                "overallScore",
                                "riskLevel",
                                method,
                                explanation,
                                "scoredAt"
                            )
                        VALUES
                            (
                                $1,
                                $2,
                                $3,
                                $4::"RiskLevel",
                                $5,
                                $6,
                                $7
                            )
                        ''',
                        str(uuid.uuid4()),
                        protocol_id,
                        overall_score,
                        risk_level,
                        method,
                        explanation,
                        snapshot_at,
                    )

                    logger.info(
                        f"Risk score: {slug} = {overall_score} "
                        f"({risk_level}) [{method}]"
                    )

                    if escalated:
                        watchers = await conn.fetch(
                            '''
                            SELECT u.email
                            FROM watchlist_items w
                            JOIN users u ON u.id = w."userId"
                            WHERE w."protocolId" = $1
                            ''',
                            protocol_id,
                        )

                        telegram_watchers = await conn.fetch(
                            '''
                            SELECT u."telegramChatId"
                            FROM watchlist_items w
                            JOIN users u ON u.id = w."userId"
                            WHERE w."protocolId" = $1
                              AND u."telegramChatId" IS NOT NULL
                            ''',
                            protocol_id,
                        )

                        for tw in telegram_watchers:
                            sent = await send_telegram_message(
                                tw["telegramChatId"],
                                (
                                    f"⚠️ {slug} risk level: "
                                    f"{risk_level} ({overall_score}/100)\\n"
                                    f"{explanation}"
                                ),
                            )

                            logger.info(
                                f"Telegram alert to "
                                f"{tw['telegramChatId']} for {slug}: "
                                f"{'sent' if sent else 'failed'}"
                            )

                        for w in watchers:
                            sent = await send_alert_email(
                                to_email=w["email"],
                                protocol_name=slug,
                                risk_level=risk_level,
                                score=overall_score,
                                explanation=explanation,
                            )

                            logger.info(
                                f"Alert email to {w['email']} for {slug}: "
                                f"{'sent' if sent else 'failed'}"
                            )

            except Exception:
                logger.exception(
                    f"Ingestion failed for {slug}, "
                    f"continuing with remaining protocols"
                )

        return written
