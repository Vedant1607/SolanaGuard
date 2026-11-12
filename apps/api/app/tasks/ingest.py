import asyncio
import uuid
import logging
from datetime import datetime, timezone

from app.db import get_pool
from app.solana.protocols import LAUNCH_PROTOCOLS, get_protocol_metrics
from app.scoring.rule_based import score_from_snapshots
from app.alerts.email import send_alert_email
from app.alerts.telegram import send_telegram_message

logger = logging.getLogger(__name__)

_ingestion_lock = asyncio.Lock()
INGESTION_ADVISORY_LOCK_ID = 8_191_424_272


async def ingest_all_protocols() -> int:
    """Fetch live metrics for every launch protocol, write a snapshot row each,
    and score each protocol from its 2 most recent snapshots.
    Returns the number of snapshots written. Skips (returns 0) if another
    local task or API instance is already ingesting."""
    if _ingestion_lock.locked():
        logger.info("Ingestion already in progress, skipping this trigger")
        return 0

    async with _ingestion_lock:
        pool = await get_pool()
        async with pool.acquire() as lock_connection:
            lock_acquired = bool(
                await lock_connection.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)", INGESTION_ADVISORY_LOCK_ID
                )
            )
            if not lock_acquired:
                logger.info("Skipping ingestion cycle; another API instance holds the lock")
                return 0

            try:
                return await _ingest_protocols(pool)
            finally:
                # This session owns the advisory lock, so release it before
                # returning the connection to the pool. PostgreSQL also
                # releases it if the session is lost.
                try:
                    await lock_connection.fetchval(
                        "SELECT pg_advisory_unlock($1::bigint)", INGESTION_ADVISORY_LOCK_ID
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to release ingestion advisory lock")


async def _send_escalation_alerts(conn, protocol_id: str, slug: str, result) -> None:
    """Notify each watcher once per channel. Email and Telegram are independent:
    a failure (or no recipients) on one channel never blocks the other."""
    try:
        email_rows = await conn.fetch(
            'SELECT DISTINCT u.email FROM watchlist_items w '
            'JOIN users u ON u.id = w."userId" '
            'WHERE w."protocolId" = $1',
            protocol_id,
        )
        for row in email_rows:
            sent = await send_alert_email(
                to_email=row["email"], protocol_name=slug,
                risk_level=result.risk_level, score=result.overall_score,
                explanation=result.explanation,
            )
            logger.info(f"Alert email to {row['email']} for {slug}: {'sent' if sent else 'failed'}")
    except Exception:
        logger.exception(f"Email alerts failed for {slug}")

    try:
        telegram_rows = await conn.fetch(
            'SELECT DISTINCT u."telegramChatId" FROM watchlist_items w '
            'JOIN users u ON u.id = w."userId" '
            'WHERE w."protocolId" = $1 AND u."telegramChatId" IS NOT NULL',
            protocol_id,
        )
        for row in telegram_rows:
            sent = await send_telegram_message(
                row["telegramChatId"],
                f"⚠️ {slug} risk level: {result.risk_level} ({result.overall_score}/100)\n{result.explanation}",
            )
            logger.info(f"Telegram alert to {row['telegramChatId']} for {slug}: {'sent' if sent else 'failed'}")
    except Exception:
        logger.exception(f"Telegram alerts failed for {slug}")


async def _score_latest_snapshots(conn, protocol_id: str, slug: str) -> None:
    """Rule-based score from the two most recent snapshots (runs after the
    snapshot INSERT has committed; failures here never undo that snapshot)."""
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
            await _send_escalation_alerts(conn, protocol_id, slug, result)
    else:
        logger.info(f"Not enough history yet for {slug} — skipping score")


ACTIVE_PROTOCOL_QUERY = 'SELECT id FROM protocols WHERE slug = $1 AND "isActive" = true'


async def _ingest_protocols(pool) -> int:
    """Perform one ingestion cycle after deployment-wide coordination succeeds.

    ``protocols.isActive`` is authoritative: a protocol that is inactive (or
    has no row) is skipped *before* any external fetch, so deactivating it in
    the database stops collection, not just display. ``LAUNCH_PROTOCOLS`` only
    supplies per-protocol source configuration (program ID, DefiLlama slug);
    active protocols without such configuration (e.g. Drift, which is
    security-monitored only) are not ingested here.
    """
    written = 0

    for slug in LAUNCH_PROTOCOLS:
        try:
            async with pool.acquire() as conn:
                protocol_id = await conn.fetchval(ACTIVE_PROTOCOL_QUERY, slug)
            if protocol_id is None:
                logger.info(f"Protocol {slug} is inactive or has no row, skipping ingestion")
                continue

            metrics = await get_protocol_metrics(slug)
            if metrics is None:
                logger.warning(f"No metrics returned for {slug}, skipping")
                continue

            async with pool.acquire() as conn:
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
                tx_label = "unavailable" if metrics.tx_count_24h is None else metrics.tx_count_24h
                logger.info(f"Snapshot written: {slug} TVL=${metrics.tvl_usd:,.0f} tx24h={tx_label}")

                try:
                    await _score_latest_snapshots(conn, protocol_id, slug)
                except Exception:
                    logger.exception(f"Snapshot saved for {slug}, but risk scoring failed")
        except Exception:
            logger.exception(f"Ingestion failed for {slug}, continuing with remaining protocols")

    return written
