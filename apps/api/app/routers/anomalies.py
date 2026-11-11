"""Read-only anomaly feed backed by persisted detector results."""

from datetime import datetime
from enum import Enum
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_pool


logger = logging.getLogger(__name__)
router = APIRouter()


class RiskLevel(str, Enum):
    """Values of the PostgreSQL ``RiskLevel`` enum."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    """Values of the PostgreSQL ``AnomalyType`` enum."""

    LIQUIDITY_WITHDRAWAL = "LIQUIDITY_WITHDRAWAL"
    TX_SPIKE = "TX_SPIKE"
    TVL_DECLINE = "TVL_DECLINE"
    WHALE_MOVEMENT = "WHALE_MOVEMENT"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    RUG_PULL_SIGNAL = "RUG_PULL_SIGNAL"


class ProtocolIdentity(BaseModel):
    id: str
    name: str
    slug: str


class DetectorMetadata(BaseModel):
    version: str
    method: str


class AnomalyRecord(BaseModel):
    id: str
    type: AnomalyType
    severity: RiskLevel
    description: str
    detected_at: datetime
    protocol: ProtocolIdentity
    calibrated_score: float
    raw_score: float | None
    threshold: float | None
    detector: DetectorMetadata


class Pagination(BaseModel):
    limit: int
    offset: int
    total: int


class AnomalyListResponse(BaseModel):
    data: list[AnomalyRecord]
    pagination: Pagination


def _where_clause(protocol_slug: str | None, severity: RiskLevel | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    if protocol_slug is not None:
        values.append(protocol_slug)
        clauses.append(f"p.slug = ${len(values)}")
    if severity is not None:
        values.append(severity.value)
        clauses.append(f'a.severity = ${len(values)}::"RiskLevel"')
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), values


@router.get(
    "",
    response_model=AnomalyListResponse,
    summary="List persisted anomalies",
    description=(
        "Returns persisted detector results, newest first. Scores are the detector's "
        "calibrated percentile, raw score, and calibration threshold."
    ),
)
async def list_anomalies(
    limit: Annotated[int, Query(ge=1, le=100, description="Maximum records to return")] = 50,
    offset: Annotated[int, Query(ge=0, description="Number of records to skip")] = 0,
    protocol_slug: Annotated[
        str | None, Query(min_length=1, max_length=255, description="Filter by protocol slug")
    ] = None,
    severity: Annotated[RiskLevel | None, Query(description="Filter by RiskLevel enum value")] = None,
) -> AnomalyListResponse:
    """List anomalies without exposing acknowledgement or user-specific data."""
    where_clause, filters = _where_clause(protocol_slug, severity)
    data_params = [*filters, limit, offset]
    limit_position = len(filters) + 1
    offset_position = len(filters) + 2
    query = f'''
        SELECT
            a.id,
            a.type::text AS type,
            a.severity::text AS severity,
            a.description,
            a."detectedAt" AS detected_at,
            a.score AS calibrated_score,
            a."rawScore" AS raw_score,
            a."scoreThreshold" AS threshold,
            a."detectorVersion" AS detector_version,
            a."detectionMethod" AS detection_method,
            p.id AS protocol_id,
            p.name AS protocol_name,
            p.slug AS protocol_slug
        FROM anomalies AS a
        JOIN protocols AS p ON p.id = a."protocolId"
        {where_clause}
        ORDER BY a."detectedAt" DESC, a.id DESC
        LIMIT ${limit_position} OFFSET ${offset_position}
    '''
    count_query = f'''
        SELECT count(*)
        FROM anomalies AS a
        JOIN protocols AS p ON p.id = a."protocolId"
        {where_clause}
    '''

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *data_params)
            total = await conn.fetchval(count_query, *filters)
    except Exception:
        logger.exception("Failed to list anomalies")
        raise HTTPException(status_code=500, detail="Unable to retrieve anomalies") from None

    data = [
        AnomalyRecord(
            id=row["id"],
            type=row["type"],
            severity=row["severity"],
            description=row["description"],
            detected_at=row["detected_at"],
            protocol=ProtocolIdentity(
                id=row["protocol_id"], name=row["protocol_name"], slug=row["protocol_slug"]
            ),
            calibrated_score=row["calibrated_score"],
            raw_score=row["raw_score"],
            threshold=row["threshold"],
            detector=DetectorMetadata(
                version=row["detector_version"], method=row["detection_method"]
            ),
        )
        for row in rows
    ]
    return AnomalyListResponse(
        data=data,
        pagination=Pagination(limit=limit, offset=offset, total=int(total)),
    )
