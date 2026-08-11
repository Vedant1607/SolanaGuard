from fastapi import APIRouter
from app.db import get_pool

router = APIRouter()


@router.get("")
async def list_protocols():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT slug, name, category, "programId", "isActive" FROM protocols ORDER BY name'
        )
    return [dict(row) for row in rows]