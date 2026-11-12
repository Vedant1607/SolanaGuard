"""Idempotent backfill of DefiLlama daily TVL history into ``metric_observations``.

For each active protocol with a DefiLlama slug:

1. Fetch and parse the history over HTTP, outside any database transaction.
2. In one short transaction per protocol, take the blocking transaction-level
   advisory lock ``pg_advisory_xact_lock`` (concurrent backfills serialise and
   the second one finds every day already stored), read the days already
   stored for this adapter and metric, and insert only the missing ones.
3. Verify that every closed source day is now stored.

Nothing is ever updated or deleted. A stored day whose value differs from the
source (DefiLlama occasionally revises history) is reported as a *conflict*
and left as it was. Only ``metric_observations`` is written.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.adapters.base import Observation, ObservationStatus
from app.adapters.defillama_history import (
    ADAPTER_KEY,
    ADAPTER_VERSION,
    HISTORY_SLUGS,
    METRIC_TVL_DAILY,
    SOURCE,
    UNIT_USD,
    DailyPoint,
    HistoryUnavailable,
    fetch_protocol_payload,
    parse_daily_tvl,
)
from app.db import get_pool
from app.tasks.security_collector import INSERT_OBSERVATION, observation_row

logger = logging.getLogger(__name__)

DEFILLAMA_BACKFILL_LOCK_ID = 8_191_424_275

ACTIVE_PROTOCOLS_QUERY = 'SELECT id, slug FROM protocols WHERE "isActive" = true ORDER BY slug'

STORED_DAYS_QUERY = """
    SELECT "sourceObservedAt" AS observed_at, "valueNumeric" AS value
    FROM metric_observations
    WHERE "protocolId" = $1 AND "adapterKey" = $2 AND "metricKey" = $3 AND status = 'OK'
    ORDER BY "sourceObservedAt", id
"""


@dataclass
class ProtocolBackfill:
    slug: str
    llama_slug: str | None
    source_points: int = 0
    closed_days: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    missing_days_in_source: int = 0
    first_day: str | None = None
    last_day: str | None = None
    inserted: int = 0
    already_stored: int = 0
    conflicts: int = 0
    stored_after: int = 0
    stored_not_in_source: int = 0
    verified: bool = False
    error: str | None = None


@dataclass
class BackfillReport:
    collected_at: str
    protocols: list[ProtocolBackfill] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        keys = ("closed_days", "inserted", "already_stored", "conflicts")
        totals = {k: sum(getattr(p, k) for p in self.protocols) for k in keys}
        totals["failed"] = sum(1 for p in self.protocols if p.error)
        totals["unverified"] = sum(1 for p in self.protocols if not p.error and not p.verified)
        return totals

    def as_dict(self) -> dict:
        return {"collected_at": self.collected_at, "totals": self.totals, "protocols": [asdict(p) for p in self.protocols]}


def plan_inserts(
    points: list[DailyPoint], stored: dict[datetime, Decimal]
) -> tuple[list[DailyPoint], int, int]:
    """(points to insert, already stored with the same value, conflicts)."""
    to_insert: list[DailyPoint] = []
    same = conflicts = 0
    for point in points:
        if point.observed_at not in stored:
            to_insert.append(point)
        elif stored[point.observed_at] == point.value_usd:
            same += 1
        else:
            conflicts += 1
    return to_insert, same, conflicts


def _rows(points: list[DailyPoint], protocol_id: str, collected_at: datetime) -> list[tuple]:
    return [
        observation_row(
            Observation(
                metric_key=METRIC_TVL_DAILY,
                status=ObservationStatus.OK,
                unit=UNIT_USD,
                source=SOURCE,
                value_numeric=point.value_usd,
                source_observed_at=point.observed_at,
            ),
            protocol_id,
            collected_at,
            ADAPTER_KEY,
            ADAPTER_VERSION,
        )
        for point in points
    ]


async def _write_protocol(pool, protocol_id: str, points: list[DailyPoint], collected_at: datetime, result: ProtocolBackfill) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", DEFILLAMA_BACKFILL_LOCK_ID)
            stored: dict[datetime, Decimal] = {}
            for row in await conn.fetch(STORED_DAYS_QUERY, protocol_id, ADAPTER_KEY, METRIC_TVL_DAILY):
                stored.setdefault(row["observed_at"], row["value"])  # keep the first if ever duplicated
            to_insert, result.already_stored, result.conflicts = plan_inserts(points, stored)
            if to_insert:
                await conn.executemany(INSERT_OBSERVATION, _rows(to_insert, protocol_id, collected_at))
            result.inserted = len(to_insert)
            after = {row["observed_at"] for row in await conn.fetch(STORED_DAYS_QUERY, protocol_id, ADAPTER_KEY, METRIC_TVL_DAILY)}
    source_days = {p.observed_at for p in points}
    result.stored_after = len(after)
    result.stored_not_in_source = len(after - source_days)
    result.verified = source_days <= after and result.conflicts == 0


async def run_backfill(
    *, pool=None, client: httpx.AsyncClient | None = None, now: datetime | None = None, slugs: list[str] | None = None
) -> BackfillReport:
    now = now or datetime.now(timezone.utc)
    collected_at = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    pool = pool or await get_pool()
    report = BackfillReport(collected_at=collected_at.isoformat())

    async with pool.acquire() as conn:
        protocols = [dict(r) for r in await conn.fetch(ACTIVE_PROTOCOLS_QUERY)]
    if slugs is not None:
        protocols = [p for p in protocols if p["slug"] in slugs]

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        for protocol in protocols:
            result = ProtocolBackfill(slug=protocol["slug"], llama_slug=HISTORY_SLUGS.get(protocol["slug"]))
            report.protocols.append(result)
            if result.llama_slug is None:
                result.error = "NO_HISTORY_SOURCE"
                continue
            try:
                parsed = parse_daily_tvl(await fetch_protocol_payload(client, result.llama_slug), now)
            except HistoryUnavailable as error:
                result.error = error.code
                logger.warning("DefiLlama history unavailable for %s (%s)", result.slug, error.code)
                continue
            result.source_points = parsed.source_points
            result.closed_days = len(parsed.points)
            result.excluded = dict(parsed.excluded)
            result.missing_days_in_source = parsed.missing_days
            if parsed.points:
                result.first_day = parsed.points[0].day.isoformat()
                result.last_day = parsed.points[-1].day.isoformat()
            try:
                await _write_protocol(pool, protocol["id"], parsed.points, collected_at, result)
            except Exception as error:  # isolate one protocol's failure
                result.error = f"WRITE_FAILED_{type(error).__name__}"
                logger.error("DefiLlama backfill write failed for %s (%s)", result.slug, type(error).__name__)
    finally:
        if own_client:
            await client.aclose()
    return report
