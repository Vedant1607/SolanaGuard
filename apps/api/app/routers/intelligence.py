"""GET /api/v1/protocols/{slug}/intelligence: daily TVL behaviour (``tvl-robust-v1``).

Computed on read from stored DefiLlama daily observations; nothing is
persisted. The result is unusualness relative to the protocol's own history
and its peers, never a probability of an exploit or compromise.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.adapters.defillama_history import ADAPTER_KEY, ADAPTER_VERSION, METRIC_TVL_DAILY, SOURCE
from app.db import get_pool
from app.ml import tvl_robust as model

logger = logging.getLogger(__name__)
router = APIRouter()

# Enough calendar days for MIN_HISTORY_DAYS observations before the latest day
# plus the trailing baseline, with margin for source gaps.
LOOKBACK_DAYS = 400
# Optional per-day history (``?history_days=N``). Capped so the earliest
# history day still has a full baseline inside LOOKBACK_DAYS; the scores are
# the ones already computed for the latest day, nothing extra is persisted.
MAX_HISTORY_DAYS = 180

PROTOCOL_QUERY = 'SELECT id, slug, name, "isActive" AS is_active FROM protocols WHERE slug = $1'

SERIES_QUERY = """
    SELECT o.id, p.slug, o."sourceObservedAt" AS observed_at, o."valueNumeric" AS value,
           o."collectedAt" AS collected_at
    FROM metric_observations o
    JOIN protocols p ON p.id = o."protocolId"
    WHERE o."adapterKey" = $1 AND o."metricKey" = $2 AND o.status = 'OK'
      AND o."sourceObservedAt" >= $3
      AND (p."isActive" = true OR p.id = $4)
    ORDER BY p.slug, o."sourceObservedAt", o.id
"""


class SupportingObservation(BaseModel):
    id: str
    role: str  # "latest_day" | "previous_day"
    day: date
    value_usd: float
    source_observed_at: datetime
    collected_at: datetime


class ObservationWindow(BaseModel):
    baseline_start: date | None
    baseline_end: date | None
    baseline_days: int
    baseline_points: int
    history_days_before_latest: int
    peers: int


class MarketContext(BaseModel):
    peer_median_return: float | None
    market_z: float | None
    market_wide: bool | None


class HistoryDay(BaseModel):
    """One stored closed day. Calendar days without an observation are not listed."""

    day: date
    observation_id: str
    excess_log_return: float | None
    daily_log_return: float | None
    peer_median_return: float | None
    robust_z: float | None
    unusual: bool  # |robust_z| >= threshold; False when unscored
    market_wide: bool | None
    reasons: list[str]  # why robust_z is None, if it is


class HistoryWindow(BaseModel):
    start: date  # first calendar day covered (inclusive)
    end: date  # last closed UTC day (yesterday, inclusive)
    days: int


class IntelligenceResponse(BaseModel):
    protocol: dict
    model: dict
    evaluated_at: datetime
    classification: str  # NORMAL | UNUSUAL | INSUFFICIENT_HISTORY
    robust_z: float | None
    threshold: float
    direction: str | None
    drawdown_30d: float | None
    excess_log_return: float | None
    daily_log_return: float | None
    market: MarketContext
    latest_closed_day: date | None
    latest_age_days: int | None
    observation_window: ObservationWindow
    source: dict
    supporting_observations: list[SupportingObservation]
    explanation: list[str]
    insufficient_reasons: list[dict]
    disclaimer: str
    history_window: HistoryWindow | None = None
    history: list[HistoryDay] = []


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def build_response(protocol, rows, now: datetime, history_days: int = 0) -> IntelligenceResponse:
    series: dict[str, list[model.DailyObservation]] = {}
    by_id = {}
    for row in rows:
        by_id[row["id"]] = row
        series.setdefault(row["slug"], []).append(
            model.DailyObservation(day=row["observed_at"].date(), value=float(row["value"]), observation_id=row["id"])
        )
    scores = model.compute_scores(series).get(protocol["slug"], []) if series else []
    latest = scores[-1] if scores else None
    today = now.date()
    classification, reasons = model.classify(latest, today)

    supporting = []
    if latest is not None:
        for role, obs_id in (("latest_day", latest.observation_id), ("previous_day", latest.previous_observation_id)):
            if obs_id and obs_id in by_id:
                row = by_id[obs_id]
                supporting.append(SupportingObservation(
                    id=obs_id, role=role, day=row["observed_at"].date(), value_usd=float(row["value"]),
                    source_observed_at=_utc(row["observed_at"]), collected_at=_utc(row["collected_at"]),
                ))

    window = None
    history: list[HistoryDay] = []
    if history_days > 0:
        end = today - timedelta(days=1)
        start = end - timedelta(days=history_days - 1)
        window = HistoryWindow(start=start, end=end, days=history_days)
        for s in scores:
            if start <= s.day <= end:
                market_wide = None if s.market_z is None else abs(s.market_z) >= model.THRESHOLD
                history.append(HistoryDay(
                    day=s.day, observation_id=s.observation_id, excess_log_return=s.excess_return,
                    daily_log_return=s.log_return, peer_median_return=s.peer_return, robust_z=s.robust_z,
                    unusual=s.robust_z is not None and abs(s.robust_z) >= model.THRESHOLD,
                    market_wide=market_wide, reasons=list(s.reasons),
                ))

    baseline_end = latest.day - timedelta(days=1) if latest else None
    baseline_start = latest.day - timedelta(days=model.BASELINE_DAYS) if latest else None
    return IntelligenceResponse(
        protocol={"slug": protocol["slug"], "name": protocol["name"], "is_active": protocol["is_active"]},
        model={"name": model.MODEL_NAME, "version": model.MODEL_VERSION, "method": model.METHOD,
               "min_history_days": model.MIN_HISTORY_DAYS, "target_flag_rate": model.TARGET_FLAG_RATE},
        evaluated_at=now,
        classification=classification,
        robust_z=latest.robust_z if latest else None,
        threshold=model.THRESHOLD,
        direction=None if not latest or latest.robust_z is None else ("decline" if latest.robust_z < 0 else "increase"),
        drawdown_30d=latest.drawdown if latest else None,
        excess_log_return=latest.excess_return if latest else None,
        daily_log_return=latest.log_return if latest else None,
        market=MarketContext(
            peer_median_return=latest.peer_return if latest else None,
            market_z=latest.market_z if latest else None,
            market_wide=None if not latest or latest.market_z is None else abs(latest.market_z) >= model.THRESHOLD,
        ),
        latest_closed_day=latest.day if latest else None,
        latest_age_days=(today - latest.day).days if latest else None,
        observation_window=ObservationWindow(
            baseline_start=baseline_start, baseline_end=baseline_end, baseline_days=model.BASELINE_DAYS,
            baseline_points=latest.baseline_points if latest else 0,
            history_days_before_latest=latest.history_days if latest else 0,
            peers=latest.peers if latest else 0,
        ),
        source={"source": SOURCE, "adapter": f"{ADAPTER_KEY}@{ADAPTER_VERSION}", "metric_key": METRIC_TVL_DAILY,
                "resolution": "closed UTC days"},
        supporting_observations=supporting,
        explanation=model.explain(latest) if latest and latest.robust_z is not None else [],
        insufficient_reasons=[{"code": code, "detail": model.REASON_TEXT.get(code, code)} for code in reasons],
        disclaimer=model.DISCLAIMER,
        history_window=window,
        history=history,
    )


@router.get("/{slug}/intelligence", response_model=IntelligenceResponse)
async def get_protocol_intelligence(
    slug: str,
    history_days: int = Query(0, ge=0, le=MAX_HISTORY_DAYS, description="Include per-day scores for this many closed days"),
) -> IntelligenceResponse:
    now = datetime.now(timezone.utc)
    since = datetime.combine(now.date() - timedelta(days=LOOKBACK_DAYS), datetime.min.time())
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            protocol = await conn.fetchrow(PROTOCOL_QUERY, slug)
            if protocol is None:
                raise HTTPException(status_code=404, detail="Protocol not found")
            rows = await conn.fetch(SERIES_QUERY, ADAPTER_KEY, METRIC_TVL_DAILY, since, protocol["id"])
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        logger.warning("Intelligence endpoint unavailable: Phase 1 migrations are not applied")
        raise HTTPException(status_code=503, detail="Intelligence data is not available yet") from None
    return build_response(protocol, rows, now, history_days)
