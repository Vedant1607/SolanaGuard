"""Recurring refresh of DefiLlama daily history (local development only).

Hosted deployments do not use this scheduler (``ENVIRONMENT=production``
rejects ``DEFILLAMA_HISTORY_REFRESH_ENABLED=true``); they run
``scripts/refresh_defillama_history.py`` as a cron job, which shares
``run_locked_jobs`` below but not the local identity gate.

Every ``DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS`` after the previous cycle
*finishes* (first cycle after ``..._STARTUP_DELAY_SECONDS``), one cycle runs
the two existing, idempotent backfills in order:

1. ``run_backfill()``        -> ``tvl.usd.daily``                      (defillama_tvl_history@1)
2. ``run_token_backfill()``  -> ``tokens.qty/usd.daily``, ``tvl.borrowed.usd.daily`` (defillama_token_history@1)

No new write path: both functions fetch outside transactions, write each
protocol in its own short transaction under their existing blocking
``pg_advisory_xact_lock``, insert only missing keys and report conflicting
source revisions instead of overwriting them.

Safety gates (fail closed):

* Disabled unless ``DEFILLAMA_HISTORY_REFRESH_ENABLED`` is true (default false).
* Before every cycle the connected database must report
  ``current_database() == DEFILLAMA_HISTORY_REFRESH_EXPECTED_DATABASE`` and
  ``pg_control_system().system_identifier ==
  DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER`` (which has no default).
  Any mismatch, missing setting or failed check skips both backfills.
* One cycle at a time: an in-process lock, plus a session-level
  ``pg_try_advisory_lock`` held only for the duration of a cycle so another API
  instance's cycle is reported as *skipped*, never as success. The blocking
  per-protocol locks inside the backfills stay as they are.

Status is kept in memory only: after an API restart nothing is known about
earlier runs until a new cycle completes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.config import settings
from app.db import get_pool
from app.tasks.defillama_backfill import run_backfill
from app.tasks.defillama_token_backfill import run_token_backfill

logger = logging.getLogger(__name__)

REFRESH_CYCLE_LOCK_ID = 8_191_424_277

IDENTITY_QUERY = "SELECT current_database() AS database, (SELECT system_identifier FROM pg_control_system())::text AS system_identifier"


@dataclass
class JobStatus:
    job: str  # "tvl_history" | "token_history"
    outcome: str | None = None  # succeeded | partial | failed
    started_at: str | None = None
    finished_at: str | None = None
    inserted: int | None = None
    already_stored: int | None = None
    conflicts: int | None = None
    failed_protocols: list[str] = field(default_factory=list)
    unverified_protocols: list[str] = field(default_factory=list)
    latest_source_day: str | None = None  # latest closed day the source reported in this run
    error: str | None = None  # exception type only, never a message that could hold a URL


@dataclass
class RefreshStatus:
    enabled: bool
    interval_seconds: int
    startup_delay_seconds: int
    state: str = "not_started"  # not_started | disabled | waiting | running | stopped
    running: bool = False
    last_attempt_started_at: str | None = None
    last_attempt_finished_at: str | None = None
    last_outcome: str | None = None  # succeeded | partial | failed | skipped_identity | skipped_locked | skipped_running | cancelled
    last_success_at: str | None = None  # last cycle in which both jobs succeeded
    last_failure: str | None = None
    consecutive_failures: int = 0
    next_run_at: str | None = None
    identity: dict = field(default_factory=dict)
    jobs: dict[str, JobStatus] = field(default_factory=dict)
    note: str = "In-memory status: unknown after an API restart until the next cycle completes."


def _new_status() -> RefreshStatus:
    return RefreshStatus(
        enabled=settings.DEFILLAMA_HISTORY_REFRESH_ENABLED,
        interval_seconds=settings.DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS,
        startup_delay_seconds=settings.DEFILLAMA_HISTORY_REFRESH_STARTUP_DELAY_SECONDS,
        state="not_started" if settings.DEFILLAMA_HISTORY_REFRESH_ENABLED else "disabled",
    )


_status = _new_status()
_cycle_lock = asyncio.Lock()

FAILED_OUTCOMES = {"failed", "skipped_identity"}


def refresh_status() -> dict:
    return asdict(_status)


def reset_status() -> None:
    """Fresh in-memory status (tests, and what a restarted API starts with)."""
    global _status
    _status = _new_status()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- identity ------------------------------------------------------------------


async def verify_database_identity(pool) -> tuple[bool, dict]:
    """(ok, details). Fails closed on any error or missing expectation."""
    expected_db = settings.DEFILLAMA_HISTORY_REFRESH_EXPECTED_DATABASE
    expected_id = settings.DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER
    details = {"checked_at": _now().isoformat(), "expected_database": expected_db or None,
               "expected_system_identifier": expected_id or None, "database": None, "system_identifier": None,
               "verified": False, "reason": None}
    if not expected_db or not expected_id:
        details["reason"] = "expected_identity_not_configured"
        return False, details
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(IDENTITY_QUERY)
    except Exception as error:  # e.g. permission denied on pg_control_system()
        details["reason"] = f"identity_check_failed:{type(error).__name__}"
        return False, details
    details["database"], details["system_identifier"] = row["database"], row["system_identifier"]
    if row["database"] != expected_db:
        details["reason"] = "database_mismatch"
    elif row["system_identifier"] != expected_id:
        details["reason"] = "system_identifier_mismatch"
    else:
        details["verified"] = True
    return details["verified"], details


# --- jobs ----------------------------------------------------------------------


def _summarise(job: JobStatus, report) -> None:
    totals = report.totals
    job.inserted, job.already_stored, job.conflicts = totals["inserted"], totals["already_stored"], totals["conflicts"]
    job.failed_protocols = sorted(p.slug for p in report.protocols if p.error)
    job.unverified_protocols = sorted(p.slug for p in report.protocols if not p.error and not p.verified)
    last_days = [p.last_day for p in report.protocols if p.last_day]
    job.latest_source_day = max(last_days) if last_days else None
    ok = [p for p in report.protocols if not p.error and p.verified]
    if not report.protocols or not ok:
        job.outcome = "failed"
    elif job.failed_protocols or job.unverified_protocols:
        job.outcome = "partial"
    else:
        job.outcome = "succeeded"


async def _run_job(name: str, fn: Callable[..., Awaitable], pool) -> JobStatus:
    job = JobStatus(job=name, started_at=_now().isoformat())
    _status.jobs[name] = job
    logger.info("DefiLlama history refresh: %s started", name)
    try:
        _summarise(job, await fn(pool=pool))
    except asyncio.CancelledError:
        job.outcome, job.error = "failed", "Cancelled"
        raise
    except Exception as error:
        job.outcome, job.error = "failed", type(error).__name__
        logger.error("DefiLlama history refresh: %s failed (%s)", name, type(error).__name__)
    finally:
        job.finished_at = _now().isoformat()
    if job.error is None:
        logger.info("DefiLlama history refresh: %s %s (inserted=%s already_stored=%s conflicts=%s failed=%s unverified=%s latest_source_day=%s)",
                    name, job.outcome, job.inserted, job.already_stored, job.conflicts, job.failed_protocols or "none",
                    job.unverified_protocols or "none", job.latest_source_day)
    return job


def _combine(tvl: JobStatus, tokens: JobStatus) -> str:
    outcomes = {tvl.outcome, tokens.outcome}
    if outcomes == {"succeeded"}:
        return "succeeded"
    if outcomes == {"failed"}:
        return "failed"
    return "partial"


async def run_locked_jobs(pool, *, tvl_job: Callable[..., Awaitable] = run_backfill,
                          token_job: Callable[..., Awaitable] = run_token_backfill) -> tuple[str, list[JobStatus]]:
    """Both backfills in order under the cycle's try-lock. Shared by the local
    scheduler (after its identity gate) and the hosted cron script
    (scripts/refresh_defillama_history.py, after its target check).

    Returns ("skipped_locked", []) when another refresh holds the lock, else the
    combined outcome and both job statuses. Job errors are captured, not raised.
    """
    async with pool.acquire() as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", REFRESH_CYCLE_LOCK_ID):
            logger.warning("DefiLlama history refresh skipped: another instance holds the refresh lock")
            return "skipped_locked", []
        try:
            tvl = await _run_job("tvl_history", tvl_job, pool)
            tokens = await _run_job("token_history", token_job, pool)
        finally:
            try:
                await lock_conn.fetchval("SELECT pg_advisory_unlock($1::bigint)", REFRESH_CYCLE_LOCK_ID)
            except Exception:
                logger.exception("Failed to release the DefiLlama refresh lock")
    return _combine(tvl, tokens), [tvl, tokens]


async def refresh_cycle(*, pool=None, tvl_job: Callable[..., Awaitable] = run_backfill,
                        token_job: Callable[..., Awaitable] = run_token_backfill) -> str:
    """One cycle. Returns the cycle outcome; never raises except on cancellation."""
    if _cycle_lock.locked():
        logger.warning("DefiLlama history refresh skipped: a cycle is already running in this process")
        _status.last_outcome = "skipped_running"
        return "skipped_running"
    async with _cycle_lock:
        _status.running, _status.state = True, "running"
        _status.last_attempt_started_at, _status.last_attempt_finished_at = _now().isoformat(), None
        _status.jobs = {}
        outcome = "failed"
        try:
            pool = pool or await get_pool()
            ok, identity = await verify_database_identity(pool)
            _status.identity = identity
            if not ok:
                outcome = "skipped_identity"
                _status.last_failure = f"database identity not verified ({identity['reason']}); no backfill was run"
                logger.error("DefiLlama history refresh blocked: database identity not verified (%s)", identity["reason"])
                return outcome
            logger.info("DefiLlama history refresh: database identity verified (%s, cluster %s)",
                        identity["database"], identity["system_identifier"])
            outcome, jobs = await run_locked_jobs(pool, tvl_job=tvl_job, token_job=token_job)
            failures = [f"{j.job}: {j.outcome}" + (f" ({j.error})" if j.error else "") for j in jobs if j.outcome != "succeeded"]
            _status.last_failure = "; ".join(failures) if failures else _status.last_failure
            return outcome
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            outcome = "failed"
            _status.last_failure = f"cycle error ({type(error).__name__})"
            logger.error("DefiLlama history refresh cycle failed (%s)", type(error).__name__)
            return outcome
        finally:
            finished = _now()
            _status.running = False
            _status.last_attempt_finished_at = finished.isoformat()
            _status.last_outcome = outcome
            if outcome == "succeeded":
                _status.last_success_at = finished.isoformat()
                _status.consecutive_failures = 0
            elif outcome in FAILED_OUTCOMES or outcome == "partial":
                _status.consecutive_failures += 1
            logger.info("DefiLlama history refresh cycle finished: %s", outcome)


async def defillama_refresh_loop(*, startup_delay: float | None = None, interval: float | None = None,
                                 cycle: Callable[[], Awaitable[str]] = refresh_cycle,
                                 sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
    """Startup delay, then cycle -> wait ``interval`` after it finishes -> cycle ... until cancelled."""
    delay = settings.DEFILLAMA_HISTORY_REFRESH_STARTUP_DELAY_SECONDS if startup_delay is None else startup_delay
    every = settings.DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS if interval is None else interval
    try:
        _status.state = "waiting"
        _status.next_run_at = (_now() + timedelta(seconds=delay)).isoformat()
        await sleep(delay)
        while True:
            _status.next_run_at = None
            await cycle()
            _status.state = "waiting"
            _status.next_run_at = (_now() + timedelta(seconds=every)).isoformat()
            await sleep(every)
    finally:
        _status.state, _status.next_run_at, _status.running = "stopped", None, False
