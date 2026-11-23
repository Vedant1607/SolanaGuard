"""GET /api/v1/history/refresh-status: DefiLlama daily-history freshness (read-only).

Two different facts, never merged:

* ``refresh``: what the in-process scheduler did (in memory; unknown after an
  API restart until a cycle completes).
* ``availability``: the latest closed UTC day actually stored per metric and
  protocol, read from ``metric_observations``. This is what the dashboard
  analysis is based on, whether or not the scheduler ran.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import asyncpg
from fastapi import APIRouter

from app.adapters.defillama_history import ADAPTER_KEY as TVL_ADAPTER, METRIC_TVL_DAILY
from app.adapters.defillama_tokens import ADAPTER_KEY as TOKEN_ADAPTER
from app.db import get_pool
from app.tasks.defillama_refresh import refresh_status

logger = logging.getLogger(__name__)
router = APIRouter()

LATEST_BY_METRIC_QUERY = """
    SELECT o."metricKey" AS metric, p.slug, max(o."sourceObservedAt")::date AS latest_day
    FROM metric_observations o JOIN protocols p ON p.id = o."protocolId"
    WHERE o."adapterKey" = ANY($1::text[]) AND o.status = 'OK' AND p."isActive" = true
    GROUP BY 1, 2
    ORDER BY 1, 2
"""


def summarise_availability(rows, today: date) -> dict:
    metrics: dict[str, dict] = {}
    for row in rows:
        m = metrics.setdefault(row["metric"], {"protocols": {}})
        m["protocols"][row["slug"]] = row["latest_day"].isoformat()
    for m in metrics.values():
        days = sorted(m["protocols"].values())
        m["latest_day"], m["oldest_latest_day"] = days[-1], days[0]
        m["lagging_protocols"] = sorted(s for s, d in m["protocols"].items() if d < days[-1])
    tvl = metrics.get(METRIC_TVL_DAILY)
    through = tvl["latest_day"] if tvl else None
    return {
        "available_through": through,  # latest closed UTC day of tvl.usd.daily stored for any active protocol
        "age_days": (today - date.fromisoformat(through)).days if through else None,
        "metrics": metrics,
    }


@router.get("/refresh-status")
async def get_refresh_status() -> dict:
    now = datetime.now(timezone.utc)
    availability: dict
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(LATEST_BY_METRIC_QUERY, [TVL_ADAPTER, TOKEN_ADAPTER])
        availability = {"ok": True, **summarise_availability(rows, now.date())}
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        availability = {"ok": False, "reason": "schema_missing"}
    except Exception as error:
        logger.warning("History availability lookup failed (%s)", type(error).__name__)
        availability = {"ok": False, "reason": "unavailable"}
    return {"evaluated_at": now.isoformat(), "refresh": refresh_status(), "availability": availability}
