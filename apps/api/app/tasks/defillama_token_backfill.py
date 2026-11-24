"""Idempotent backfill of DefiLlama per-asset token history (``defillama_token_history@1``).

For each active protocol in ``TOKEN_HISTORY_SLUGS`` (Jito, Marinade, Kamino,
Drift; inactive protocols such as Marginfi are never selected):

1. Fetch and parse the payload over HTTP, outside any database transaction.
2. In one transaction per protocol, take the blocking transaction-level
   advisory lock ``pg_advisory_xact_lock`` (concurrent runs serialise; the
   second finds everything stored), read the keys already stored for this
   adapter, and insert only the missing ones.
3. Re-read and verify that every parsed source key is stored.

``metric_observations`` has no unique constraint, so idempotency comes from
the lock plus the per-key existence check, exactly as in the TVL backfill.
Keys are (day, asset symbol) for ``tokens.*`` metrics and (day,) for the
borrowed total. Nothing is ever updated or deleted: a stored key whose value
differs from the source is reported as a *conflict* and left as it was.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.adapters.base import Observation, ObservationStatus
from app.adapters.defillama_history import HistoryUnavailable, fetch_protocol_payload
from app.adapters.defillama_tokens import (
    ADAPTER_KEY,
    ADAPTER_VERSION,
    BORROWED_HISTORY_SLUGS,
    METRIC_BORROWED_USD,
    METRIC_TOKEN_QTY,
    METRIC_TOKEN_USD,
    SOURCE,
    TOKEN_HISTORY_SLUGS,
    UNIT_TOKEN,
    UNIT_USD,
    parse_borrowed_history,
    parse_token_history,
)
from app.db import get_pool
from app.tasks.security_collector import INSERT_OBSERVATION, observation_row

logger = logging.getLogger(__name__)

DEFILLAMA_TOKEN_BACKFILL_LOCK_ID = 8_191_424_276

ACTIVE_PROTOCOLS_QUERY = 'SELECT id, slug FROM protocols WHERE "isActive" = true ORDER BY slug'

STORED_KEYS_QUERY = """
    SELECT "metricKey" AS metric, "sourceObservedAt" AS observed_at, "valueText" AS symbol, "valueNumeric" AS value
    FROM metric_observations
    WHERE "protocolId" = $1 AND "adapterKey" = $2 AND status = 'OK'
    ORDER BY "sourceObservedAt", id
"""

Key = tuple[datetime, str | None]


@dataclass(frozen=True)
class Planned:
    metric: str
    unit: str
    observed_at: datetime
    symbol: str | None
    value: Decimal


@dataclass
class MetricBackfill:
    metric: str
    unit: str
    source_keys: int = 0
    inserted: int = 0
    already_stored: int = 0
    conflicts: int = 0
    stored_after: int = 0
    stored_not_in_source: int = 0
    verified: bool = False


@dataclass
class ProtocolTokenBackfill:
    slug: str
    llama_slug: str | None
    source_points: int = 0
    closed_days: int = 0
    first_day: str | None = None
    last_day: str | None = None
    missing_days_in_source: int = 0
    assets: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    unpriced_asset_days: int = 0
    borrowed_excluded: dict[str, int] = field(default_factory=dict)
    metrics: list[MetricBackfill] = field(default_factory=list)
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.error is None and bool(self.metrics) and all(m.verified for m in self.metrics)


@dataclass
class TokenBackfillReport:
    collected_at: str
    protocols: list[ProtocolTokenBackfill] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        metrics = [m for p in self.protocols for m in p.metrics]
        totals = {k: sum(getattr(m, k) for m in metrics) for k in ("source_keys", "inserted", "already_stored", "conflicts")}
        totals["excluded"] = sum(sum(p.excluded.values()) + sum(p.borrowed_excluded.values()) for p in self.protocols)
        totals["failed"] = sum(1 for p in self.protocols if p.error)
        totals["unverified"] = sum(1 for p in self.protocols if not p.error and not p.verified)
        return totals

    def as_dict(self) -> dict:
        return {"collected_at": self.collected_at, "adapter": f"{ADAPTER_KEY}@{ADAPTER_VERSION}", "totals": self.totals,
                "protocols": [{**asdict(p), "verified": p.verified} for p in self.protocols]}


def plan_metric(planned: list[Planned], stored: dict[Key, Decimal]) -> tuple[list[Planned], int, int]:
    """(rows to insert, already stored with the same value, conflicts) for one metric."""
    to_insert: list[Planned] = []
    same = conflicts = 0
    for item in planned:
        key = (item.observed_at, item.symbol)
        if key not in stored:
            to_insert.append(item)
        elif stored[key] == item.value:
            same += 1
        else:
            conflicts += 1
    return to_insert, same, conflicts


def build_plan(slug: str, payload: object, now: datetime, result: ProtocolTokenBackfill) -> dict[str, list[Planned]]:
    """Interpret the payload into planned observations per metric (no I/O)."""
    parsed = parse_token_history(payload, now)
    result.source_points = parsed.source_points
    result.excluded = dict(parsed.excluded)
    result.unpriced_asset_days = parsed.unpriced
    days = parsed.days
    result.closed_days = len(days)
    result.assets = len(parsed.symbols)
    result.missing_days_in_source = parsed.missing_days
    if days:
        result.first_day, result.last_day = days[0].isoformat(), days[-1].isoformat()
    plan: dict[str, list[Planned]] = {
        METRIC_TOKEN_QTY: [Planned(METRIC_TOKEN_QTY, UNIT_TOKEN, p.observed_at, p.symbol, p.quantity) for p in parsed.points],
        METRIC_TOKEN_USD: [Planned(METRIC_TOKEN_USD, UNIT_USD, p.observed_at, p.symbol, p.value_usd) for p in parsed.points if p.value_usd is not None],
    }
    if slug in BORROWED_HISTORY_SLUGS:
        borrowed = parse_borrowed_history(payload, now)
        result.borrowed_excluded = dict(borrowed.excluded)
        plan[METRIC_BORROWED_USD] = [Planned(METRIC_BORROWED_USD, UNIT_USD, p.observed_at, None, p.value_usd) for p in borrowed.points]
    return plan


def _row(item: Planned, protocol_id: str, collected_at: datetime) -> tuple:
    return observation_row(
        Observation(metric_key=item.metric, status=ObservationStatus.OK, unit=item.unit, source=SOURCE,
                    value_numeric=item.value, value_text=item.symbol, source_observed_at=item.observed_at),
        protocol_id, collected_at, ADAPTER_KEY, ADAPTER_VERSION,
    )


def _stored(rows) -> dict[str, dict[Key, Decimal]]:
    out: dict[str, dict[Key, Decimal]] = {}
    for row in rows:
        out.setdefault(row["metric"], {}).setdefault((row["observed_at"], row["symbol"]), row["value"])
    return out


async def _write_protocol(pool, protocol_id: str, plan: dict[str, list[Planned]], collected_at: datetime, result: ProtocolTokenBackfill) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", DEFILLAMA_TOKEN_BACKFILL_LOCK_ID)
            stored = _stored(await conn.fetch(STORED_KEYS_QUERY, protocol_id, ADAPTER_KEY))
            rows = []
            for metric, planned in plan.items():
                to_insert, same, conflicts = plan_metric(planned, stored.get(metric, {}))
                unit = planned[0].unit if planned else (UNIT_TOKEN if metric == METRIC_TOKEN_QTY else UNIT_USD)
                result.metrics.append(MetricBackfill(metric=metric, unit=unit, source_keys=len(planned),
                                                     inserted=len(to_insert), already_stored=same, conflicts=conflicts))
                rows.extend(_row(item, protocol_id, collected_at) for item in to_insert)
            if rows:
                await conn.executemany(INSERT_OBSERVATION, rows)
            after = _stored(await conn.fetch(STORED_KEYS_QUERY, protocol_id, ADAPTER_KEY))
    for m in result.metrics:
        source_keys = {(i.observed_at, i.symbol) for i in plan[m.metric]}
        stored_keys = set(after.get(m.metric, {}))
        m.stored_after = len(stored_keys)
        m.stored_not_in_source = len(stored_keys - source_keys)
        m.verified = source_keys <= stored_keys and m.conflicts == 0


async def run_token_backfill(
    *, pool=None, client: httpx.AsyncClient | None = None, now: datetime | None = None, slugs: list[str] | None = None
) -> TokenBackfillReport:
    now = now or datetime.now(timezone.utc)
    collected_at = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    pool = pool or await get_pool()
    report = TokenBackfillReport(collected_at=collected_at.isoformat())

    async with pool.acquire() as conn:
        protocols = [dict(r) for r in await conn.fetch(ACTIVE_PROTOCOLS_QUERY) if r["slug"] in TOKEN_HISTORY_SLUGS]
    if slugs is not None:
        protocols = [p for p in protocols if p["slug"] in slugs]

    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        for protocol in protocols:
            result = ProtocolTokenBackfill(slug=protocol["slug"], llama_slug=TOKEN_HISTORY_SLUGS.get(protocol["slug"]))
            report.protocols.append(result)
            try:
                plan = build_plan(result.slug, await fetch_protocol_payload(client, result.llama_slug), now, result)
            except HistoryUnavailable as error:
                result.error = error.code
                logger.warning("DefiLlama token history unavailable for %s (%s)", result.slug, error.code)
                continue
            try:
                await _write_protocol(pool, protocol["id"], plan, collected_at, result)
            except Exception as error:  # isolate one protocol's failure; its transaction rolled back
                result.metrics = []
                result.error = f"WRITE_FAILED_{type(error).__name__}"
                logger.error("DefiLlama token backfill write failed for %s (%s)", result.slug, type(error).__name__)
    finally:
        if own_client:
            await client.aclose()
    return report
