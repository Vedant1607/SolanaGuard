"""DefiLlama daily per-asset token history (``defillama_token_history@1``).

Source: ``https://api.llama.fi/protocol/{slug}`` -> ``chainTvls["Solana"]``,
whose ``tokens`` array holds per-asset **quantities** (native units, keyed by
DefiLlama's symbol) and ``tokensInUsd`` the same assets valued in USD, one
point per UTC day at 00:00 plus one provisional intraday point. Kamino's
``chainTvls["Solana-borrowed"]["tvl"]`` holds the daily borrowed USD total.

Interpretation only; persistence lives in ``app.tasks.defillama_token_backfill``.

Rules (same closed-day rules as ``defillama_history``):

* Only closed, midnight-aligned UTC days are kept; missing days stay missing.
* A quantity must be a finite number > 0. Zero is excluded (DefiLlama lists
  emptied assets as 0 and it cannot be told apart from "not reported"), and so
  are negative quantities: lending protocols report deposits minus borrows per
  asset, which can go negative through source accounting. Every exclusion is
  counted, never stored as a substitute value.
* Quantities are never converted to USD. A USD value is kept beside the
  quantity only when the source gives a finite value > 0 for the same asset
  and day; otherwise the asset is "unpriced" for that day.
* Symbols are kept exactly as DefiLlama reports them (e.g. ``SOL`` and
  ``WSOL`` stay separate assets).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from app.adapters.defillama_history import (
    HISTORY_SLUGS,
    SECONDS_PER_DAY,
    HistoryUnavailable,
    ParseResult,
    parse_daily_tvl,
)

ADAPTER_KEY = "defillama_token_history"
ADAPTER_VERSION = "1"
SOURCE = "defillama"
CHAIN_KEY = "Solana"
BORROWED_CHAIN_KEY = "Solana-borrowed"

METRIC_TOKEN_QTY = "tokens.qty.daily"  # unit "token": native units of the asset in valueText
METRIC_TOKEN_USD = "tokens.usd.daily"  # unit "usd": DefiLlama's USD value of that asset that day
METRIC_BORROWED_USD = "tvl.borrowed.usd.daily"  # unit "usd": Kamino only
UNIT_TOKEN = "token"
UNIT_USD = "usd"
MAX_SYMBOL_LENGTH = 64

# Protocols with a genuine per-asset token history on DefiLlama (verified
# 2026-11-12: Raydium and Orca publish none). Marginfi is inactive.
TOKEN_HISTORY_SLUGS: dict[str, str] = {slug: HISTORY_SLUGS[slug] for slug in ("jito", "marinade", "kamino", "drift")}
BORROWED_HISTORY_SLUGS = frozenset({"kamino"})


@dataclass(frozen=True)
class TokenPoint:
    day: date
    observed_at: datetime  # naive UTC midnight
    symbol: str
    quantity: Decimal
    value_usd: Decimal | None  # None when the source gave no usable USD value


@dataclass
class TokenParseResult:
    points: list[TokenPoint] = field(default_factory=list)
    source_points: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    unpriced: int = 0  # kept quantities without a usable same-day USD value

    @property
    def days(self) -> list[date]:
        return sorted({p.day for p in self.points})

    @property
    def symbols(self) -> list[str]:
        return sorted({p.symbol for p in self.points})

    @property
    def missing_days(self) -> int:
        days = self.days
        if len(days) < 2:
            return 0
        return (days[-1] - days[0]).days + 1 - len(days)


def _decimal(value: object) -> Decimal | None:
    """A finite non-bool number as Decimal, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return Decimal(str(value)) if isinstance(value, float) else Decimal(value)


def _closed_day(stamp: object, today: date, excluded: Counter[str]) -> date | None:
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        excluded["malformed_point"] += 1
        return None
    stamp = int(stamp)
    if stamp % SECONDS_PER_DAY != 0:
        excluded["intraday_point"] += 1
        return None
    day = datetime.fromtimestamp(stamp, timezone.utc).date()
    if day >= today:
        excluded["open_day"] += 1
        return None
    return day


def _chain(payload: object, key: str) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("chainTvls"), dict):
        raise HistoryUnavailable("MALFORMED_PAYLOAD")
    chain = payload["chainTvls"].get(key)
    if chain is None:
        raise HistoryUnavailable("NO_CHAIN_SERIES")
    if not isinstance(chain, dict):
        raise HistoryUnavailable("MALFORMED_PAYLOAD")
    return chain


def _by_day(series: object, today: date, excluded: Counter[str], label: str) -> dict[date, dict]:
    """{closed day: {symbol: raw value}}; a day listed twice keeps neither."""
    if series is None:
        return {}
    if not isinstance(series, list):
        raise HistoryUnavailable("MALFORMED_PAYLOAD")
    days: dict[date, list[dict]] = {}
    for point in series:
        if not isinstance(point, dict) or not isinstance(point.get("tokens"), dict):
            excluded[f"malformed_{label}_point"] += 1
            continue
        day = _closed_day(point.get("date"), today, excluded if label == "quantity" else Counter())
        if day is not None:
            days.setdefault(day, []).append(point["tokens"])
    out = {}
    for day, entries in days.items():
        if len(entries) > 1:
            excluded[f"duplicate_{label}_day"] += 1
            continue
        out[day] = entries[0]
    return out


def parse_token_history(payload: object, now: datetime, chain_key: str = CHAIN_KEY) -> TokenParseResult:
    """Closed-day per-asset quantities (and same-day USD values) for one chain."""
    chain = _chain(payload, chain_key)
    quantities = chain.get("tokens")
    if not isinstance(quantities, list):
        raise HistoryUnavailable("NO_TOKEN_SERIES")
    today = now.astimezone(timezone.utc).date() if now.tzinfo else now.date()
    result = TokenParseResult(source_points=len(quantities))
    qty_days = _by_day(quantities, today, result.excluded, "quantity")
    usd_days = _by_day(chain.get("tokensInUsd"), today, result.excluded, "usd")

    for day in sorted(qty_days):
        usd_tokens = usd_days.get(day, {})
        observed_at = datetime(day.year, day.month, day.day)
        for raw_symbol, raw_quantity in sorted(qty_days[day].items(), key=lambda kv: str(kv[0])):
            symbol = raw_symbol.strip() if isinstance(raw_symbol, str) else ""
            if not symbol or len(symbol) > MAX_SYMBOL_LENGTH:
                result.excluded["invalid_symbol"] += 1
                continue
            quantity = _decimal(raw_quantity)
            if quantity is None:
                result.excluded["invalid_quantity"] += 1
                continue
            if quantity < 0:
                result.excluded["negative_quantity"] += 1
                continue
            if quantity == 0:
                result.excluded["zero_quantity"] += 1
                continue
            value_usd = _decimal(usd_tokens.get(raw_symbol))
            if value_usd is None or value_usd <= 0:
                result.unpriced += 1
                value_usd = None
            result.points.append(TokenPoint(day, observed_at, symbol, quantity, value_usd))
    return result


def parse_borrowed_history(payload: object, now: datetime) -> ParseResult:
    """Kamino's closed-day borrowed USD total from ``chainTvls["Solana-borrowed"]["tvl"]``."""
    chain = _chain(payload, BORROWED_CHAIN_KEY)
    return parse_daily_tvl({"tvl": chain.get("tvl")}, now)
