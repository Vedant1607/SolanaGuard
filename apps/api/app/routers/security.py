"""GET /api/v1/protocols/{slug}/security: registry, raw observations and evidence.

Observations and interpretation are returned separately: each evidence item
references the exact observation rows (id, source, slot, timestamps) it was
derived from. Levels are explained, never weighted, and missing data yields
UNKNOWN.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_pool
from app.risk.evidence import DimensionAssessment, Evidence, ObservationRecord
from app.risk.security_assessment import evaluate_protocol_security

logger = logging.getLogger(__name__)
router = APIRouter()

PROTOCOL_QUERY = """
    SELECT id, slug, name, category::text AS category, "isActive" AS is_active,
           "supportLevel"::text AS support_level
    FROM protocols
    WHERE slug = $1
"""


class ObservationOut(BaseModel):
    id: str
    metric_key: str
    status: str
    value: str | None
    unit: str
    source: str
    source_slot: int | None
    source_observed_at: datetime | None
    collected_at: datetime
    adapter: str
    error_code: str | None


class AddressOut(BaseModel):
    id: str
    address: str
    kind: str
    label: str
    is_shared_dependency: bool
    expected_owner: str | None
    verification: str
    verified_at: datetime | None
    verification_note: str | None


class EvidenceOut(BaseModel):
    rule_id: str
    indicator: str
    level: str
    explanation: str
    address_id: str | None
    address_label: str | None
    observations: list[ObservationOut]
    rule_version: str


class DimensionOut(BaseModel):
    dimension: str
    level: str
    explanation: str
    assessed_indicators: int
    total_indicators: int
    rule_version: str


class ProtocolOut(BaseModel):
    slug: str
    name: str
    category: str
    is_active: bool
    support_level: str


class SecurityResponse(BaseModel):
    protocol: ProtocolOut
    evaluated_at: datetime
    dimension: DimensionOut
    evidence: list[EvidenceOut]
    addresses: list[AddressOut]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _observation_out(record: ObservationRecord) -> ObservationOut:
    return ObservationOut(
        id=record.id,
        metric_key=record.metric_key,
        status=record.status,
        value=record.display_value,
        unit=record.unit,
        source=record.source,
        source_slot=record.source_slot,
        source_observed_at=_utc(record.source_observed_at),
        collected_at=_utc(record.collected_at),
        adapter=f"{record.adapter_key}@{record.adapter_version}",
        error_code=record.error_code,
    )


def _evidence_out(evidence: Evidence) -> EvidenceOut:
    return EvidenceOut(
        rule_id=evidence.rule_id,
        indicator=evidence.indicator,
        level=evidence.level.value,
        explanation=evidence.explanation,
        address_id=evidence.address.id if evidence.address else None,
        address_label=evidence.address.label if evidence.address else None,
        observations=[_observation_out(o) for o in evidence.observations],
        rule_version=evidence.rule_version,
    )


def build_response(protocol_row, assessment: DimensionAssessment, address_rows, now: datetime) -> SecurityResponse:
    return SecurityResponse(
        protocol=ProtocolOut(
            slug=protocol_row["slug"],
            name=protocol_row["name"],
            category=protocol_row["category"],
            is_active=protocol_row["is_active"],
            support_level=protocol_row["support_level"],
        ),
        evaluated_at=now,
        dimension=DimensionOut(
            dimension=assessment.dimension,
            level=assessment.level.value,
            explanation=assessment.explanation,
            assessed_indicators=assessment.assessed,
            total_indicators=assessment.total,
            rule_version=assessment.rule_version,
        ),
        evidence=[_evidence_out(e) for e in assessment.evidence],
        addresses=[
            AddressOut(**{**dict(row), "verified_at": _utc(row["verified_at"])}) for row in address_rows
        ],
    )


@router.get("/{slug}/security", response_model=SecurityResponse)
async def get_protocol_security(slug: str) -> SecurityResponse:
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            protocol = await conn.fetchrow(PROTOCOL_QUERY, slug)
            if protocol is None:
                raise HTTPException(status_code=404, detail="Protocol not found")
            assessment, address_rows = await evaluate_protocol_security(conn, protocol["id"], now)
    except (
        asyncpg.exceptions.UndefinedTableError,
        asyncpg.exceptions.UndefinedColumnError,
        asyncpg.exceptions.UndefinedObjectError,
    ):
        logger.warning("Security endpoint unavailable: Phase 1 migrations are not applied")
        raise HTTPException(status_code=503, detail="Security data is not available yet") from None
    return build_response(protocol, assessment, address_rows, now)
