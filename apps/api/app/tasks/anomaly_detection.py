"""Recurring, coordinated execution of the persisted anomaly detector."""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.db import get_pool
from app.ml.anomaly_detector import run_detector


logger = logging.getLogger(__name__)

# A stable, application-specific PostgreSQL advisory-lock key. The lock is
# session-scoped and is released explicitly (or by PostgreSQL if its session
# ends), so it coordinates detector runs across API instances without schema
# changes or another service.
DETECTOR_ADVISORY_LOCK_ID = 8_191_424_271


async def run_scheduled_detector_once() -> bool:
    """Run the detector once when this process obtains the deployment-wide lock.

    ``False`` means another API instance currently owns the detector lock. A
    detector failure is logged and also returns ``False`` so the recurrence can
    continue at its next interval.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as connection:
            lock_acquired = bool(
                await connection.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)", DETECTOR_ADVISORY_LOCK_ID
                )
            )
            if not lock_acquired:
                logger.info("Skipping anomaly detector run; another API instance holds the lock")
                return False

            try:
                report = await run_detector()
                logger.info(
                    "Anomaly detector run complete: snapshots_processed=%s "
                    "anomalies_inserted=%s duplicate_records_skipped=%s persistence_failures=%s",
                    report.snapshots_processed,
                    report.anomalies_inserted,
                    report.duplicate_anomalies_skipped,
                    report.persistence_failures,
                )
                return True
            finally:
                # Advisory locks are owned by the connection session, so this
                # must execute before its acquire context returns it to the pool.
                try:
                    await connection.fetchval(
                        "SELECT pg_advisory_unlock($1::bigint)", DETECTOR_ADVISORY_LOCK_ID
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to release anomaly detector advisory lock")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Scheduled anomaly detector run failed")
        return False


async def anomaly_detector_loop() -> None:
    """Run independently from ingestion, retrying failures on the next interval."""
    while True:
        await run_scheduled_detector_once()
        await asyncio.sleep(settings.ANOMALY_DETECTOR_INTERVAL_SECONDS)
