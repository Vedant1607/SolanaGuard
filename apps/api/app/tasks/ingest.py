import uuid
import logging
from datetime import datetime, timezone

from app.db import get_pool
from app.solana.protocols import LAUNCH_PROTOCOLS, get_protocol_metrics

logger = logging.getLogger(__name__)


async def ingest_all_protocols() -> int:
    """Fetch live metrics for every launch protocol and write a snapshot row each.
    Returns the number of snapshots written."""
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
        except Exception:
            logger.exception(f"Ingestion failed for {slug}, continuing with remaining protocols")
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

    return written