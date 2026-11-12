"""Scheduled collection of on-chain security observations.

Three phases, so no database transaction is held open during RPC calls:

1. **Plan** (short transaction): take the transaction-level advisory lock with
   ``pg_try_advisory_xact_lock`` (skip the run if another instance holds it),
   read the active registry, and find protocols this adapter already collected
   within ``MIN_RUN_SPACING``. Those are skipped, so an API restart or a second
   instance does not collect the same cycle again.
2. **Collect** (no transaction, no pooled connection held): run the adapter per
   protocol against the RPC endpoint, each bounded by a timeout. An adapter
   crash affects only that protocol.
3. **Persist** (one short transaction per protocol): take the blocking
   ``pg_advisory_xact_lock`` so writers serialise, re-check that no other
   instance wrote this protocol meanwhile, then insert its observations. A
   failed write rolls back only that protocol.

Transaction-level locks are released automatically at commit/rollback, so this
works through transaction-pooling proxies such as Neon's pooler, where
session-level locks are unreliable. Only active protocols and active registry
addresses are collected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import asyncpg
import httpx

from app.adapters.base import AdapterContext, AddressKind, Observation, RegisteredAddress
from app.adapters.onchain_security import OnchainSecurityAdapter
from app.config import settings
from app.db import get_pool
from app.solana.account_reader import PUBLIC_MAINNET_RPC_URL, AccountReader, resolve_security_rpc_url

logger = logging.getLogger(__name__)

SECURITY_COLLECTOR_ADVISORY_LOCK_ID = 8_191_424_273
PROTOCOL_COLLECTION_TIMEOUT_SECONDS = 120.0
RPC_REQUEST_TIMEOUT_SECONDS = 10.0


def min_run_spacing() -> timedelta:
    """A protocol collected more recently than this is not collected again."""
    return timedelta(seconds=settings.SECURITY_COLLECTOR_INTERVAL_SECONDS / 2)


REGISTRY_QUERY = """
    SELECT p.id AS protocol_id, p.slug,
           a.id AS address_id, a.address, a.kind::text AS kind, a.label,
           a."isSharedDependency" AS is_shared_dependency,
           a."expectedOwner" AS expected_owner
    FROM protocols p
    JOIN protocol_addresses a ON a."protocolId" = p.id
    WHERE p."isActive" = true AND a."isActive" = true
    ORDER BY p.slug, a.kind, a.address
"""

INSERT_OBSERVATION = """
    INSERT INTO metric_observations
        (id, "protocolId", "addressId", "metricKey", status, "valueNumeric", "valueText",
         unit, source, "sourceObservedAt", "sourceSlot", "collectedAt",
         "adapterKey", "adapterVersion", "errorCode")
    VALUES ($1, $2, $3, $4, $5::"ObservationStatus", $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""


RECENTLY_COLLECTED_QUERY = """
    SELECT DISTINCT "protocolId" AS protocol_id
    FROM metric_observations
    WHERE "adapterKey" = $1 AND "collectedAt" >= $2
"""

PROTOCOL_RECENTLY_COLLECTED_QUERY = """
    SELECT EXISTS (
        SELECT 1 FROM metric_observations
        WHERE "protocolId" = $1 AND "adapterKey" = $2 AND "collectedAt" >= $3
    )
"""


@dataclass
class CollectorReport:
    ran: bool
    skipped_reason: str | None = None
    protocols_collected: int = 0
    observations_written: int = 0
    failed_protocols: list[str] = field(default_factory=list)
    # Collected recently (this or another instance), so not collected again.
    recently_collected: list[str] = field(default_factory=list)


# Run outcomes, as reported by ``run_outcome`` and the heartbeat:
#   succeeded          every due protocol was collected and written
#   partial            some protocols written, some failed (see failed_protocols)
#   failed             protocols were due but none could be collected or written
#   recently_collected nothing was due: all collected within MIN_RUN_SPACING
#                      (by this or another instance)
#   locked / schema_missing / no_rpc_endpoint
#                      the run was skipped before collecting (``skipped_reason``)
#   error              the run raised unexpectedly (heartbeat only)
FAILED_OUTCOMES = frozenset({"failed", "error"})


def run_outcome(report: CollectorReport) -> str:
    if not report.ran:
        return report.skipped_reason or "skipped"
    if report.protocols_collected == 0:
        return "failed" if report.failed_protocols else "recently_collected"
    return "partial" if report.failed_protocols else "succeeded"


@dataclass
class CollectorStatus:
    """Heartbeat of this process's collector loop (per API instance)."""

    enabled: bool
    interval_seconds: int
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_outcome: str | None = None  # see run_outcome; None before the first run
    last_report: dict | None = None
    next_run_at: datetime | None = None
    # Consecutive runs whose outcome was "failed" or "error"; reset by any other outcome.
    consecutive_failures: int = 0


_status = CollectorStatus(
    enabled=settings.SECURITY_COLLECTOR_ENABLED,
    interval_seconds=settings.SECURITY_COLLECTOR_INTERVAL_SECONDS,
)


def collector_state(status: CollectorStatus, rpc_configured: bool) -> str:
    """One word for the loop's state. An explicitly disabled collector is
    reported as such, never as a missing RPC endpoint."""
    if not status.enabled:
        return "disabled"
    if not rpc_configured:
        return "no_rpc_endpoint"
    if status.last_started_at is None:
        return "starting"
    if status.last_outcome in FAILED_OUTCOMES:
        return "failing"
    return "active"


def collector_status() -> dict:
    """Heartbeat plus the endpoint as currently configured (host only)."""
    rpc_configured, rpc_host, public_fallback = _describe_endpoint(resolve_security_rpc_url())
    return {
        **asdict(_status),
        "state": collector_state(_status, rpc_configured),
        "rpc_configured": rpc_configured,
        "rpc_host": rpc_host,
        "public_fallback": public_fallback,
    }


def _describe_endpoint(url: str | None) -> tuple[bool, str | None, bool]:
    """(configured, host only, public fallback). Never the full URL: it may embed a key."""
    if url is None:
        return False, None, False
    try:
        host = httpx.URL(url).host or None
    except Exception:  # an unparsable URL is reported without echoing it
        host = None
    return True, host, url == PUBLIC_MAINNET_RPC_URL


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def observation_row(
    observation: Observation, protocol_id: str, collected_at: datetime, adapter_key: str, adapter_version: str
) -> tuple:
    return (
        str(uuid.uuid4()),
        protocol_id,
        observation.address_id,
        observation.metric_key,
        observation.status.value,
        observation.value_numeric,
        observation.value_text,
        observation.unit,
        observation.source,
        _naive_utc(observation.source_observed_at),
        observation.source_slot,
        _naive_utc(collected_at),
        adapter_key,
        adapter_version,
        observation.error_code,
    )


def group_registry(rows, now: datetime) -> list[AdapterContext]:
    grouped: dict[tuple[str, str], list[RegisteredAddress]] = defaultdict(list)
    for row in rows:
        grouped[(row["protocol_id"], row["slug"])].append(
            RegisteredAddress(
                id=row["address_id"],
                address=row["address"],
                kind=AddressKind(row["kind"]),
                label=row["label"],
                is_shared_dependency=row["is_shared_dependency"],
                expected_owner=row["expected_owner"],
            )
        )
    return [
        AdapterContext(protocol_id=protocol_id, slug=slug, addresses=tuple(addresses), now=now)
        for (protocol_id, slug), addresses in grouped.items()
    ]


async def _plan(conn, adapter_key: str, now: datetime):
    """Phase 1, inside a short transaction. Returns None if another instance holds the lock."""
    locked = await conn.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", SECURITY_COLLECTOR_ADVISORY_LOCK_ID)
    if not locked:
        return None
    registry = await conn.fetch(REGISTRY_QUERY)
    recent = await conn.fetch(RECENTLY_COLLECTED_QUERY, adapter_key, _naive_utc(now - min_run_spacing()))
    return registry, {row["protocol_id"] for row in recent}


async def _persist(pool, ctx: AdapterContext, rows: list[tuple], adapter_key: str, now: datetime) -> bool:
    """Phase 3 for one protocol. False if another instance already wrote this cycle."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", SECURITY_COLLECTOR_ADVISORY_LOCK_ID)
            already = await conn.fetchval(
                PROTOCOL_RECENTLY_COLLECTED_QUERY, ctx.protocol_id, adapter_key, _naive_utc(now - min_run_spacing())
            )
            if already:
                return False
            await conn.executemany(INSERT_OBSERVATION, rows)
    return True


async def collect_security_observations_once(
    *,
    pool=None,
    adapter_factory: Callable[[httpx.AsyncClient, str], object] | None = None,
) -> CollectorReport:
    rpc_url = resolve_security_rpc_url()
    if rpc_url is None:
        logger.error(
            "Security collector idle: SOLANA_RPC_URL is not configured and the public "
            "endpoint is only used when DEBUG is true"
        )
        return CollectorReport(ran=False, skipped_reason="no_rpc_endpoint")

    if adapter_factory is None:
        adapter_factory = lambda client, url: OnchainSecurityAdapter(AccountReader(url, client))  # noqa: E731

    pool = pool or await get_pool()
    report = CollectorReport(ran=True)
    async with httpx.AsyncClient(timeout=RPC_REQUEST_TIMEOUT_SECONDS) as client:
        adapter = adapter_factory(client, rpc_url)
        now = datetime.now(timezone.utc)
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    planned = await _plan(conn, adapter.key, now)
        except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedObjectError):
            logger.warning("Security collector idle: registry tables are missing; apply the Phase 1 migrations")
            return CollectorReport(ran=False, skipped_reason="schema_missing")
        if planned is None:
            logger.info("Skipping security collection; another API instance holds the lock")
            return CollectorReport(ran=False, skipped_reason="locked")

        registry, recent_ids = planned
        contexts = group_registry(registry, now)
        report.recently_collected = sorted(c.slug for c in contexts if c.protocol_id in recent_ids)
        due = [c for c in contexts if c.protocol_id not in recent_ids]
        if not due:
            report.ran = False
            report.skipped_reason = "recently_collected"
            return report

        for ctx in due:
            try:
                observations = await asyncio.wait_for(adapter.collect(ctx), PROTOCOL_COLLECTION_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Adapters should not raise; if one does, write nothing for this
                # protocol. Its evidence then turns UNKNOWN through staleness.
                logger.error("Security collection failed for %s (%s)", ctx.slug, type(error).__name__)
                report.failed_protocols.append(ctx.slug)
                continue

            collected_at = datetime.now(timezone.utc)
            rows = [
                observation_row(o, ctx.protocol_id, collected_at, adapter.key, adapter.version) for o in observations
            ]
            try:
                written = await _persist(pool, ctx, rows, adapter.key, now)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Writing security observations failed for %s (%s)", ctx.slug, type(error).__name__)
                report.failed_protocols.append(ctx.slug)
                continue
            if not written:
                report.recently_collected.append(ctx.slug)
                continue
            report.protocols_collected += 1
            report.observations_written += len(rows)
    return report


def _record_outcome(outcome: str, report: dict) -> None:
    _status.last_outcome = outcome
    _status.last_report = report
    _status.consecutive_failures = _status.consecutive_failures + 1 if outcome in FAILED_OUTCOMES else 0


async def security_collector_loop() -> None:
    while True:
        _status.last_started_at = datetime.now(timezone.utc)
        try:
            report = await collect_security_observations_once()
            outcome = run_outcome(report)
            _record_outcome(outcome, asdict(report))
            if report.ran:
                log = logger.error if outcome == "failed" else logger.warning if outcome == "partial" else logger.info
                log(
                    "Security collection %s: protocols=%s observations=%s failed=%s recently_collected=%s",
                    outcome,
                    report.protocols_collected,
                    report.observations_written,
                    ",".join(report.failed_protocols) or "none",
                    ",".join(report.recently_collected) or "none",
                )
            elif report.skipped_reason == "recently_collected":
                logger.info("Security collection skipped: every protocol was collected within the last %s", min_run_spacing())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _record_outcome("error", {"error": type(error).__name__})
            logger.exception("Security collection run failed")
        _status.last_finished_at = datetime.now(timezone.utc)
        _status.next_run_at = _status.last_finished_at + timedelta(seconds=settings.SECURITY_COLLECTOR_INTERVAL_SECONDS)
        await asyncio.sleep(settings.SECURITY_COLLECTOR_INTERVAL_SECONDS)
