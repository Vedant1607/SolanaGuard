from fastapi import APIRouter
from app.db import get_pool
from app.tasks.ingest import ingest_all_protocols

router = APIRouter()


@router.get("")
async def list_protocols():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT slug, name, category, "programId", "isActive" FROM protocols ORDER BY name'
        )
    return [dict(row) for row in rows]

@router.post("/ingest")
async def trigger_ingestion():
    count = await ingest_all_protocols()
    return {"snapshots_written": count}

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