"""Scheduled collection of on-chain security observations.

Coordination uses a *transaction-level* advisory lock
(``pg_try_advisory_xact_lock``): it is released automatically when the
transaction ends, so it works through transaction-pooling proxies such as
Neon's pooler, where session-level locks are unreliable.

One outer transaction holds the lock for the whole run. Each protocol's batch
is written inside its own savepoint, so a failure for one protocol rolls back
only that protocol's rows. Only active protocols and active registry
addresses are collected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import asyncpg
import httpx

from app.adapters.base import AdapterContext, AddressKind, Observation, RegisteredAddress
from app.adapters.onchain_security import OnchainSecurityAdapter
from app.config import settings
from app.db import get_pool
from app.solana.account_reader import AccountReader, resolve_security_rpc_url

logger = logging.getLogger(__name__)

SECURITY_COLLECTOR_ADVISORY_LOCK_ID = 8_191_424_273
PROTOCOL_COLLECTION_TIMEOUT_SECONDS = 120.0
RPC_REQUEST_TIMEOUT_SECONDS = 10.0

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


@dataclass
class CollectorReport:
    ran: bool
    skipped_reason: str | None = None
    protocols_collected: int = 0
    observations_written: int = 0
    failed_protocols: list[str] = field(default_factory=list)


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


async def _collect_with_adapter(conn, adapter, report: CollectorReport) -> None:
    now = datetime.now(timezone.utc)
    contexts = group_registry(await conn.fetch(REGISTRY_QUERY), now)
    for ctx in contexts:
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
            async with conn.transaction():  # savepoint inside the locked outer transaction
                await conn.executemany(INSERT_OBSERVATION, rows)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("Writing security observations failed for %s (%s)", ctx.slug, type(error).__name__)
            report.failed_protocols.append(ctx.slug)
            continue
        report.protocols_collected += 1
        report.observations_written += len(rows)


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
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                locked = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1::bigint)", SECURITY_COLLECTOR_ADVISORY_LOCK_ID
                )
                if not locked:
                    logger.info("Skipping security collection; another API instance holds the lock")
                    return CollectorReport(ran=False, skipped_reason="locked")
                async with httpx.AsyncClient(timeout=RPC_REQUEST_TIMEOUT_SECONDS) as client:
                    await _collect_with_adapter(conn, adapter_factory(client, rpc_url), report)
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedObjectError):
        logger.warning("Security collector idle: registry tables are missing; apply the Phase 1 migrations")
        return CollectorReport(ran=False, skipped_reason="schema_missing")
    return report


async def security_collector_loop() -> None:
    while True:
        try:
            report = await collect_security_observations_once()
            if report.ran:
                logger.info(
                    "Security collection complete: protocols=%s observations=%s failed=%s",
                    report.protocols_collected,
                    report.observations_written,
                    ",".join(report.failed_protocols) or "none",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Security collection run failed")
        await asyncio.sleep(settings.SECURITY_COLLECTOR_INTERVAL_SECONDS)
