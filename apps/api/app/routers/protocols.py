from fastapi import APIRouter
from app.db import get_pool
from app.tasks.ingest import ingest_all_protocols
from app.alerts.email import send_alert_email

router = APIRouter()


@router.get("")
async def list_protocols():
    """Every active protocol with its latest snapshot + latest risk score
    joined in — this is what the dashboard's protocol grid consumes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT
                p.slug, p.name, p.category,
                s."tvlUsd", s."txCount24h", s."snapshotAt",
                rs."overallScore", rs."riskLevel", rs.explanation, rs."scoredAt"
            FROM protocols p
            LEFT JOIN LATERAL (
                SELECT "tvlUsd", "txCount24h", "snapshotAt"
                FROM protocol_snapshots
                WHERE "protocolId" = p.id
                ORDER BY "snapshotAt" DESC
                LIMIT 1
            ) s ON true
            LEFT JOIN LATERAL (
                SELECT "overallScore", "riskLevel", explanation, "scoredAt"
                FROM risk_scores
                WHERE "protocolId" = p.id
                ORDER BY "scoredAt" DESC
                LIMIT 1
            ) rs ON true
            WHERE p."isActive" = true
            ORDER BY p.name
            '''
        )
    return [dict(row) for row in rows]


@router.get("/{slug}/risk-score")
async def get_latest_risk_score(slug: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''
            SELECT rs."overallScore", rs."riskLevel", rs.method, rs.explanation, rs."scoredAt"
            FROM risk_scores rs
            JOIN protocols p ON p.id = rs."protocolId"
            WHERE p.slug = $1
            ORDER BY rs."scoredAt" DESC
            LIMIT 1
            ''',
            slug,
        )
        if row is None:
            return {"detail": "No risk score yet for this protocol"}
        return dict(row)


@router.post("/ingest")
async def trigger_ingestion():
    count = await ingest_all_protocols()
    return {"snapshots_written": count}

@router.post("/{slug}/test-alert")
async def test_alert(slug: str, to_email: str):
    sent = await send_alert_email(
        to_email=to_email, protocol_name=slug,
        risk_level="HIGH", score=72.5,
        explanation="This is a test alert — not a real risk event.",
    )
    return {"sent": sent}