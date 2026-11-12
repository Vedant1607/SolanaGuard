"""DefiLlama daily TVL history as protocol-level observations.

Uses the same ``https://api.llama.fi/protocol/{slug}`` endpoint that 5-minute
ingestion already downloads (ingestion keeps only its last point). The
response's top-level ``tvl`` array holds one point per UTC day stamped at
00:00 UTC, followed by one provisional intraday point for the current day.

Only **closed days** are kept: a point must be midnight-aligned and dated
before the current UTC day. The intraday point and today's provisional 00:00
point are excluded; a later run picks today up once it is final. Missing days
stay missing (nothing is interpolated) and invalid values (non-numeric,
non-finite, zero or negative) are dropped and counted, never stored as zero.

Each stored observation keeps two times: ``sourceObservedAt`` is the day the
source measured (00:00 UTC of that day) and ``collectedAt`` is when
SolanaGuard fetched it.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx

from app.solana.protocols import LAUNCH_PROTOCOLS

ADAPTER_KEY = "defillama_tvl_history"
ADAPTER_VERSION = "1"
SOURCE = "defillama"
METRIC_TVL_DAILY = "tvl.usd.daily"
UNIT_USD = "usd"
PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"
REQUEST_TIMEOUT_SECONDS = 30.0

# SolanaGuard slug -> DefiLlama slug. Ingestion's map is reused; Drift is
# history-only (it has no 5-minute ingestion) and is listed on DefiLlama as
# "drift-trade".
HISTORY_SLUGS: dict[str, str] = {
    **{slug: cfg["llama_slug"] for slug, cfg in LAUNCH_PROTOCOLS.items()},
    "drift": "drift-trade",
}

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class DailyPoint:
    day: date
    observed_at: datetime  # naive UTC midnight, like the rest of the schema
    value_usd: Decimal


@dataclass
class ParseResult:
    points: list[DailyPoint] = field(default_factory=list)
    source_points: int = 0
    excluded: Counter[str] = field(default_factory=Counter)

    @property
    def missing_days(self) -> int:
        """Calendar days between the first and last closed point with no point."""
        if len(self.points) < 2:
            return 0
        span = (self.points[-1].day - self.points[0].day).days + 1
        return span - len(self.points)


class HistoryUnavailable(Exception):
    """The source could not provide a usable history. ``code`` is log-safe."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_daily_tvl(payload: object, now: datetime) -> ParseResult:
    """Closed daily TVL points from a DefiLlama ``/protocol`` payload.

    ``now`` decides which UTC day is still open. Points are returned in date
    order, one per day; a day appearing twice keeps neither value, because
    the source is ambiguous about which one is right.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("tvl"), list):
        raise HistoryUnavailable("MALFORMED_PAYLOAD")
    series = payload["tvl"]
    today = now.astimezone(timezone.utc).date() if now.tzinfo else now.date()
    result = ParseResult(source_points=len(series))
    by_day: dict[date, list[Decimal]] = {}

    for point in series:
        if not isinstance(point, dict):
            result.excluded["malformed_point"] += 1
            continue
        stamp, value = point.get("date"), point.get("totalLiquidityUSD")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            result.excluded["malformed_point"] += 1
            continue
        stamp = int(stamp)
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        if stamp % SECONDS_PER_DAY != 0:
            result.excluded["intraday_point"] += 1
            continue
        if day >= today:
            result.excluded["open_day"] += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            result.excluded["invalid_value"] += 1
            continue
        by_day.setdefault(day, []).append(Decimal(str(value)) if isinstance(value, float) else Decimal(value))

    for day in sorted(by_day):
        values = by_day[day]
        if len(values) > 1:
            result.excluded["duplicate_day"] += len(values)
            continue
        result.points.append(
            DailyPoint(day=day, observed_at=datetime(day.year, day.month, day.day), value_usd=values[0])
        )
    return result


async def fetch_protocol_payload(client: httpx.AsyncClient, llama_slug: str) -> object:
    """Fetch the raw payload. Errors are raised as ``HistoryUnavailable`` with a short code."""
    try:
        response = await client.get(PROTOCOL_URL.format(slug=llama_slug), timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        raise HistoryUnavailable("TIMEOUT") from None
    except httpx.HTTPError:
        raise HistoryUnavailable("NETWORK") from None
    if response.status_code != 200:
        raise HistoryUnavailable(f"HTTP_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise HistoryUnavailable("MALFORMED_PAYLOAD") from None
