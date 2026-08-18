import asyncio
import uuid
import logging
from datetime import datetime, timezone

from app.db import get_pool
from app.solana.protocols import LAUNCH_PROTOCOLS, get_protocol_metrics
from app.scoring.rule_based import score_from_snapshots
from app.alerts.email import send_alert_email

logger = logging.getLogger(__name__)

_ingestion_lock = asyncio.Lock()


async def ingest_all_protocols() -> int:
    """Fetch live metrics for every launch protocol, write a snapshot row each,
    and score each protocol from its 2 most recent snapshots.
    Returns the number of snapshots written. Skips (returns 0) if a cycle is already running."""
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
                    protocol_id = await conn.fetchval('SELECT id FROM protocols WHERE slug = $1', slug)
                    if protocol_id is None:
                        logger.warning(f"No protocol row for slug={slug}, skipping")
                        continue

                    await conn.execute(
                        '''
                        INSERT INTO protocol_snapshots
                            (id, "protocolId", "tvlUsd", "txCount24h", "snapshotAt")
                        VALUES ($1, $2, $3, $4, $5)
                        ''',
                        str(uuid.uuid4()),
                        protocol_id,
                        metrics.tvl_usd,
                        metrics.tx_count_24h,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    written += 1
                    logger.info(f"Snapshot written: {slug} TVL=${metrics.tvl_usd:,.0f} tx24h={metrics.tx_count_24h}")

                    rows = await conn.fetch(
                        'SELECT "tvlUsd", "txCount24h" FROM protocol_snapshots '
                        'WHERE "protocolId" = $1 ORDER BY "snapshotAt" DESC LIMIT 2',
                        protocol_id,
                    )
                    if len(rows) == 2:
                        latest, previous = rows[0], rows[1]
                        result = score_from_snapshots(
                            latest_tvl=latest["tvlUsd"], previous_tvl=previous["tvlUsd"],
                            latest_tx=latest["txCount24h"], previous_tx=previous["txCount24h"],
                        )
                        previous_level_row = await conn.fetchrow(
                            'SELECT "riskLevel" FROM risk_scores WHERE "protocolId" = $1 '
                            'ORDER BY "scoredAt" DESC LIMIT 1',
                            protocol_id,
                        )
                        previous_level = previous_level_row["riskLevel"] if previous_level_row else None
                        escalated = (
                            result.risk_level in ("HIGH", "CRITICAL")
                            and result.risk_level != previous_level
                        )
                        await conn.execute(
                            '''
                            INSERT INTO risk_scores
                                (id, "protocolId", "overallScore", "riskLevel", method, explanation, "scoredAt")
                            VALUES ($1, $2, $3, $4::"RiskLevel", $5, $6, $7)
                            ''',
                            str(uuid.uuid4()), protocol_id, result.overall_score, result.risk_level,
                            "rule_based", result.explanation, datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                        logger.info(f"Risk score: {slug} = {result.overall_score} ({result.risk_level})")
                        if escalated:
                            watchers = await conn.fetch(
                                'SELECT u.email FROM watchlist_items w '
                                'JOIN users u ON u.id = w."userId" '
                                'WHERE w."protocolId" = $1',
                                protocol_id,
                            )
                            for w in watchers:
                                sent = await send_alert_email(
                                    to_email=w["email"], protocol_name=slug,
                                    risk_level=result.risk_level, score=result.overall_score,
                                    explanation=result.explanation,
                                )
                                logger.info(f"Alert email to {w['email']} for {slug}: {'sent' if sent else 'failed'}")
                    else:
                        logger.info(f"Not enough history yet for {slug} — skipping score")
            except Exception:
                logger.exception(f"Ingestion failed for {slug}, continuing with remaining protocols")

        return written