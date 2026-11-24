"""Load stored security observations for one protocol and evaluate them."""

from __future__ import annotations

from datetime import datetime, timezone

from app.adapters.base import AddressKind, RegisteredAddress
from app.risk.evidence import DimensionAssessment, ObservationRecord
from app.risk.security_rules import (
    CHANGE_TRACKED_METRICS,
    HISTORY_WINDOW,
    SECURITY_METRICS,
    AddressHistory,
    ObservedChange,
    assess_security,
)

_OBSERVATION_COLUMNS = """
    o.id, o."addressId" AS address_id, o."metricKey" AS metric_key, o.status::text AS status,
    o."valueNumeric" AS value_numeric, o."valueText" AS value_text, o.unit, o.source,
    o."sourceSlot" AS source_slot, o."sourceObservedAt" AS source_observed_at,
    o."collectedAt" AS collected_at, o."adapterKey" AS adapter_key,
    o."adapterVersion" AS adapter_version, o."errorCode" AS error_code
"""

ADDRESSES_QUERY = """
    SELECT id, address, kind::text AS kind, label, "isSharedDependency" AS is_shared_dependency,
           "expectedOwner" AS expected_owner, verification::text AS verification,
           "verifiedAt" AS verified_at, "verificationNote" AS verification_note
    FROM protocol_addresses
    WHERE "protocolId" = $1 AND "isActive" = true
    ORDER BY kind, label
"""

LATEST_QUERY = f"""
    SELECT DISTINCT ON (o."addressId", o."metricKey") {_OBSERVATION_COLUMNS}
    FROM metric_observations o
    WHERE o."protocolId" = $1 AND o."addressId" IS NOT NULL AND o."metricKey" = ANY($2::text[])
    ORDER BY o."addressId", o."metricKey", o."collectedAt" DESC, o.id DESC
"""

FIRST_OK_QUERY = f"""
    SELECT DISTINCT ON (o."addressId", o."metricKey") {_OBSERVATION_COLUMNS},
           COUNT(*) OVER (PARTITION BY o."addressId", o."metricKey") AS ok_count
    FROM metric_observations o
    WHERE o."protocolId" = $1 AND o."addressId" IS NOT NULL AND o."metricKey" = ANY($2::text[])
      AND o.status = 'OK' AND o."collectedAt" >= $3
    ORDER BY o."addressId", o."metricKey", o."collectedAt" ASC, o.id ASC
"""

# Most recent value change between consecutive successful readings, per address/metric.
CHANGES_QUERY = f"""
    WITH ok AS (
        SELECT {_OBSERVATION_COLUMNS},
               LAG(o.id) OVER w AS previous_id,
               LAG(o."valueText") OVER w AS previous_text,
               LAG(o."valueNumeric") OVER w AS previous_numeric
        FROM metric_observations o
        WHERE o."protocolId" = $1 AND o."addressId" IS NOT NULL AND o."metricKey" = ANY($2::text[])
          AND o.status = 'OK' AND o."collectedAt" >= $3
        WINDOW w AS (PARTITION BY o."addressId", o."metricKey" ORDER BY o."collectedAt", o.id)
    )
    SELECT DISTINCT ON (address_id, metric_key) *
    FROM ok
    WHERE previous_id IS NOT NULL
      AND (previous_text IS DISTINCT FROM value_text OR previous_numeric IS DISTINCT FROM value_numeric)
    ORDER BY address_id, metric_key, collected_at DESC, id DESC
"""

BY_ID_QUERY = f"""
    SELECT {_OBSERVATION_COLUMNS}
    FROM metric_observations o
    WHERE o.id = ANY($1::text[])
"""


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def to_record(row) -> ObservationRecord:
    return ObservationRecord(
        id=row["id"],
        metric_key=row["metric_key"],
        status=row["status"],
        value_numeric=row["value_numeric"],
        value_text=row["value_text"],
        unit=row["unit"],
        source=row["source"],
        source_slot=row["source_slot"],
        source_observed_at=row["source_observed_at"],
        collected_at=row["collected_at"],
        adapter_key=row["adapter_key"],
        adapter_version=row["adapter_version"],
        error_code=row["error_code"],
    )


async def load_address_histories(conn, protocol_id: str, now: datetime) -> tuple[list[AddressHistory], list]:
    address_rows = await conn.fetch(ADDRESSES_QUERY, protocol_id)
    histories: dict[str, AddressHistory] = {
        row["id"]: AddressHistory(
            address=RegisteredAddress(
                id=row["id"],
                address=row["address"],
                kind=AddressKind(row["kind"]),
                label=row["label"],
                is_shared_dependency=row["is_shared_dependency"],
                expected_owner=row["expected_owner"],
            )
        )
        for row in address_rows
    }
    if not histories:
        return [], address_rows

    window_start = _naive_utc(now - HISTORY_WINDOW)
    metrics = list(SECURITY_METRICS)

    for row in await conn.fetch(LATEST_QUERY, protocol_id, metrics):
        if row["address_id"] in histories:
            histories[row["address_id"]].latest[row["metric_key"]] = to_record(row)

    for row in await conn.fetch(FIRST_OK_QUERY, protocol_id, metrics, window_start):
        if row["address_id"] in histories:
            history = histories[row["address_id"]]
            history.first_ok[row["metric_key"]] = to_record(row)
            history.ok_count[row["metric_key"]] = int(row["ok_count"])

    change_rows = await conn.fetch(CHANGES_QUERY, protocol_id, list(CHANGE_TRACKED_METRICS), window_start)
    if change_rows:
        previous = {
            row["id"]: to_record(row)
            for row in await conn.fetch(BY_ID_QUERY, [r["previous_id"] for r in change_rows])
        }
        for row in change_rows:
            if row["address_id"] in histories and row["previous_id"] in previous:
                histories[row["address_id"]].last_change[row["metric_key"]] = ObservedChange(
                    previous=previous[row["previous_id"]], current=to_record(row)
                )

    return list(histories.values()), address_rows


async def evaluate_protocol_security(conn, protocol_id: str, now: datetime) -> tuple[DimensionAssessment, list]:
    histories, address_rows = await load_address_histories(conn, protocol_id, now)
    return assess_security(histories, now), address_rows
