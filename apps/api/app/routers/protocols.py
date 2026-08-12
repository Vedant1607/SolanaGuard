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