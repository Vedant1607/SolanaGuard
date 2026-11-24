"""Daily multivariate features (``features-daily-v1``), pure and deterministic.

No database or network access: callers pass stored observations in, features
come out. Nothing here changes ``tvl-robust-v1``; the peer handling below is
specific to these features.

Conventions
-----------
* Day ``t`` uses observations dated ``<= t`` only. Baselines are strictly
  trailing (``[t-90, t-1]``), so the day being described never sits in its own
  baseline, and appending later days never changes earlier features.
* Missing days stay missing. A feature that cannot be computed is ``None`` and
  its reason is recorded in ``unavailable``; nothing is zero-filled.
* Returns are natural-log returns. "Robust z" is ``(x - median) / scale`` over
  the trailing baseline, with ``scale = 1.4826 * MAD`` and a fallback of
  ``sqrt(pi/2) * mean absolute deviation`` when the MAD is 0.

Features (per protocol and UTC day)
-----------------------------------
TVL behaviour (USD TVL from ``tvl.usd.daily``):
  ``tvl_log_return``, ``excess_return`` against a category-aware reference
  (liquid staking: the SOL price return, since their TVL is SOL quantity x SOL
  price; other protocols: leave-one-out median of the other non-liquid-staking
  protocols), ``excess_return_z``, ``drawdown_30d`` (vs the high of the
  preceding 30 days) and ``vol_ratio_7_90`` (robust scale of the excess return
  over the 7 days ending today divided by that over the preceding 90 days).

Price-neutral flow (``tokens.qty.daily`` + ``tokens.usd.daily``):
  ``flow_log`` = log of the protocol's holdings valued at *yesterday's* prices
  (today's quantities over yesterday's quantities, per asset, weighted by
  yesterday's USD value). Price moves cancel, so it measures deposits and
  withdrawals. Assets are never summed across units; each asset's contribution
  is kept in ``flow_contributions``. Assets that vanish, appear or are
  unpriced are reported, and if matched assets cover less than
  ``MIN_FLOW_COVERAGE`` of yesterday's value the flow is unavailable.
  ``flow_z`` is its trailing robust z. ``non_flow_effect = tvl_log_return - flow_log``
  is the part of the USD change the matched flow does not explain: price moves
  *plus* changes in assets that were removed, added or unpriced. It is not a
  pure price effect (e.g. Kamino reports some per-asset quantities as negative
  at source; those assets drop out of the matched flow and land here).

Lending (Kamino; ``tvl.borrowed.usd.daily``):
  DefiLlama's default lending TVL is supplied minus borrowed (verified: equal
  to ``chainTvls.Solana`` on every day), so ``utilisation = borrowed /
  (borrowed + tvl)``. Also ``borrowed_log_change``, ``utilisation_change`` and
  its trailing robust z.

Guards
------
* ``MIN_BASE_USD``: percentage features need a previous-day base of at least
  this much, so residual balances cannot produce huge spurious percentages.
* ``REGIME_BREAKS``: from the listed day on, a protocol's USD TVL no longer
  means what it did (Drift Trade kept only a residual balance from
  2026-04-02). Such days get ``regime = "residual"``: TVL-comparison features
  are unavailable and the protocol is excluded from every peer reference.
  Flow features stay governed by the base guard, so the outflow on the break
  day itself remains visible while the residual afterwards is not scored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping

import numpy as np

FEATURE_VERSION = "features-daily-v1"

BASELINE_DAYS = 90
MIN_BASELINE_POINTS = 60
SHORT_VOL_DAYS = 7
MIN_SHORT_VOL_POINTS = 6
DRAWDOWN_DAYS = 30
MIN_DRAWDOWN_POINTS = 20
MIN_PEERS = 2  # non-liquid-staking peers left after leave-one-out (Raydium, Orca, Kamino, Drift)
MIN_BASE_USD = 10_000_000.0
MIN_FLOW_COVERAGE = 0.90
MAX_CONTRIBUTIONS = 10
MAD_SCALE = 1.4826
MEAN_AD_SCALE = math.sqrt(math.pi / 2)

LIQUID_STAKING = "LIQUID_STAKING"
REGIME_BREAKS: dict[str, date] = {"drift": date(2026, 4, 2)}
# Implied SOL price (USD value / quantity of "SOL"), first source with both days.
SOL_PRICE_SOURCES: tuple[str, ...] = ("jito", "marinade")


@dataclass(frozen=True)
class AssetDay:
    quantity: float
    value_usd: float | None  # None: unpriced that day


@dataclass
class ProtocolInputs:
    slug: str
    category: str
    tvl: dict[date, float] = field(default_factory=dict)
    tokens: dict[date, dict[str, AssetDay]] = field(default_factory=dict)
    borrowed: dict[date, float] = field(default_factory=dict)


@dataclass
class DailyFeatures:
    slug: str
    day: date
    regime: str = "normal"  # "normal" | "residual"
    tvl_usd: float | None = None
    tvl_log_return: float | None = None
    reference: str | None = None  # "sol_price" | "peer_median"
    reference_return: float | None = None
    peers: int = 0
    excess_return: float | None = None
    excess_return_z: float | None = None
    drawdown_30d: float | None = None
    vol_ratio_7_90: float | None = None
    sol_log_return: float | None = None
    flow_log: float | None = None
    flow_z: float | None = None
    flow_coverage: float | None = None
    flow_removed_share: float | None = None
    flow_added_share: float | None = None
    flow_unpriced_assets: int = 0
    flow_contributions: tuple[tuple[str, float], ...] = ()
    non_flow_effect: float | None = None
    borrowed_usd: float | None = None
    borrowed_log_change: float | None = None
    utilisation: float | None = None
    utilisation_change: float | None = None
    utilisation_change_z: float | None = None
    unavailable: dict[str, str] = field(default_factory=dict)


# --- helpers -----------------------------------------------------------------

def robust_z(value: float, baseline: Iterable[float]) -> tuple[float | None, str | None]:
    """(z, reason). Baseline must already exclude the day being scored."""
    base = np.asarray([b for b in baseline if b is not None and math.isfinite(b)], dtype=float)
    if base.size < MIN_BASELINE_POINTS:
        return None, "insufficient_baseline"
    scale = _robust_scale(base)
    if scale is None:
        return None, "zero_dispersion"
    return (value - float(np.median(base))) / scale, None


def _robust_scale(values: np.ndarray) -> float | None:
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad > 0:
        return MAD_SCALE * mad
    mean_ad = float(np.mean(np.abs(values - median)))
    return MEAN_AD_SCALE * mean_ad if mean_ad > 0 else None


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _log_ratio(now: float | None, before: float | None) -> float | None:
    return math.log(now / before) if _positive(now) and _positive(before) else None


def _window(series: Mapping[date, float | None], day: date, start: int, end: int) -> list[float]:
    """Values for days day-start .. day-end (inclusive offsets), skipping missing ones."""
    out = []
    for k in range(end, start + 1):
        v = series.get(day - timedelta(days=k))
        if v is not None:
            out.append(v)
    return out


def implied_prices(tokens: Mapping[date, Mapping[str, AssetDay]], symbol: str) -> dict[date, float]:
    """USD value / quantity of one asset per day, where both are genuine and positive."""
    out = {}
    for day, assets in tokens.items():
        a = assets.get(symbol)
        if a is not None and _positive(a.quantity) and _positive(a.value_usd):
            out[day] = a.value_usd / a.quantity
    return out


def sol_log_returns(protocols: Mapping[str, ProtocolInputs], sources: tuple[str, ...] = SOL_PRICE_SOURCES) -> dict[date, float]:
    """Daily SOL log return from the first source that has both days (never mixes sources)."""
    prices = [implied_prices(protocols[s].tokens, "SOL") for s in sources if s in protocols]
    days = sorted(set().union(*prices)) if prices else []
    out = {}
    for day in days:
        prev = day - timedelta(days=1)
        for series in prices:
            if day in series and prev in series:
                out[day] = math.log(series[day] / series[prev])
                break
    return out


def fixed_price_flow(today: Mapping[str, AssetDay], yesterday: Mapping[str, AssetDay]) -> dict:
    """Price-neutral flow between two consecutive days, valued at yesterday's prices."""
    priced = {s: a for s, a in yesterday.items() if _positive(a.quantity) and _positive(a.value_usd)}
    unpriced = sum(1 for s, a in yesterday.items() if s not in priced)
    total = sum(a.value_usd for a in priced.values())
    matched = {s: a for s, a in priced.items() if s in today and _positive(today[s].quantity)}
    matched_value = sum(a.value_usd for a in matched.values())
    added = [s for s in today if s not in yesterday]
    added_value = sum(today[s].value_usd for s in added if _positive(today[s].value_usd))
    result = {"base_usd": total, "coverage": None, "removed_share": None, "added_share": None,
              "unpriced": unpriced, "flow_log": None, "contributions": ()}
    if total <= 0:
        return result
    result["coverage"] = matched_value / total
    result["removed_share"] = 1.0 - result["coverage"]
    result["added_share"] = added_value / total
    if matched_value <= 0:
        return result
    contributions = []
    revalued = 0.0
    for s, a in matched.items():
        price = a.value_usd / a.quantity
        revalued += price * today[s].quantity
        contributions.append((s, price * (today[s].quantity - a.quantity) / matched_value))
    result["flow_log"] = math.log(revalued / matched_value) if revalued > 0 else None
    contributions.sort(key=lambda c: (-abs(c[1]), c[0]))
    result["contributions"] = tuple(contributions[:MAX_CONTRIBUTIONS])
    return result


# --- main computation ----------------------------------------------------------

def compute_daily_features(
    protocols: Mapping[str, ProtocolInputs],
    *,
    regime_breaks: Mapping[str, date] = REGIME_BREAKS,
    min_base_usd: float = MIN_BASE_USD,
) -> dict[str, list[DailyFeatures]]:
    """Features for every day on which a protocol has a TVL, token or borrowed observation."""
    slugs = sorted(protocols)
    sol = sol_log_returns(protocols)

    def residual(slug: str, day: date) -> bool:
        brk = regime_breaks.get(slug)
        return brk is not None and day >= brk

    # Pass 1: own TVL log returns (guarded) for every protocol and day.
    returns: dict[str, dict[date, float]] = {}
    for slug in slugs:
        tvl = protocols[slug].tvl
        own = {}
        for day in tvl:
            prev = tvl.get(day - timedelta(days=1))
            if not residual(slug, day) and _positive(prev) and prev >= min_base_usd:
                r = _log_ratio(tvl[day], prev)
                if r is not None:
                    own[day] = r
        returns[slug] = own

    non_lst = [s for s in slugs if protocols[s].category != LIQUID_STAKING]
    results: dict[str, list[DailyFeatures]] = {}
    for slug in slugs:
        p = protocols[slug]
        days = sorted(set(p.tvl) | set(p.tokens) | set(p.borrowed))
        excess: dict[date, float] = {}
        flows: dict[date, float] = {}
        util: dict[date, float] = {}
        util_change: dict[date, float] = {}
        out: list[DailyFeatures] = []
        for day in days:
            f = DailyFeatures(slug=slug, day=day, tvl_usd=p.tvl.get(day), sol_log_return=sol.get(day))
            u = f.unavailable
            prev_day = day - timedelta(days=1)
            if residual(slug, day):
                f.regime = "residual"

            # TVL behaviour
            if f.regime == "residual":
                for name in ("tvl_log_return", "excess_return", "excess_return_z", "drawdown_30d", "vol_ratio_7_90"):
                    u[name] = "regime_break"
            elif day not in p.tvl:
                for name in ("tvl_log_return", "excess_return", "excess_return_z", "drawdown_30d", "vol_ratio_7_90"):
                    u[name] = "no_observation"
            else:
                f.tvl_log_return = returns[slug].get(day)
                if f.tvl_log_return is None:
                    prev = p.tvl.get(prev_day)
                    u["tvl_log_return"] = "previous_day_missing" if prev is None else "below_min_base"
                if p.category == LIQUID_STAKING:
                    f.reference, f.reference_return = "sol_price", sol.get(day)
                    if f.reference_return is None:
                        u.setdefault("excess_return", "no_reference")
                else:
                    f.reference = "peer_median"
                    peer_returns = [returns[o][day] for o in non_lst if o != slug and day in returns[o]]
                    f.peers = len(peer_returns)
                    if f.peers >= MIN_PEERS:
                        f.reference_return = float(np.median(peer_returns))
                    else:
                        u.setdefault("excess_return", "insufficient_peers")
                if f.tvl_log_return is not None and f.reference_return is not None:
                    f.excess_return = f.tvl_log_return - f.reference_return
                    excess[day] = f.excess_return
                    f.excess_return_z, reason = robust_z(f.excess_return, _window(excess, day, BASELINE_DAYS, 1))
                    if reason:
                        u["excess_return_z"] = reason
                else:
                    u.setdefault("excess_return", u.get("tvl_log_return", "unavailable"))
                    u["excess_return_z"] = u["excess_return"]
                window = [v for k in range(1, DRAWDOWN_DAYS + 1)
                          if (v := p.tvl.get(day - timedelta(days=k))) is not None and not residual(slug, day - timedelta(days=k))]
                if len(window) < MIN_DRAWDOWN_POINTS:
                    u["drawdown_30d"] = "insufficient_baseline"
                elif max(window) < min_base_usd:
                    u["drawdown_30d"] = "below_min_base"
                else:
                    f.drawdown_30d = p.tvl[day] / max(window) - 1.0
                short = _window(excess, day, SHORT_VOL_DAYS - 1, 0)
                long = _window(excess, day, BASELINE_DAYS, 1)
                if len(short) < MIN_SHORT_VOL_POINTS or len(long) < MIN_BASELINE_POINTS:
                    u["vol_ratio_7_90"] = "insufficient_baseline"
                else:
                    s_scale, l_scale = _robust_scale(np.asarray(short)), _robust_scale(np.asarray(long))
                    if l_scale is None:
                        u["vol_ratio_7_90"] = "zero_dispersion"
                    else:
                        f.vol_ratio_7_90 = (s_scale or 0.0) / l_scale

            # Price-neutral flow
            if not p.tokens:
                u["flow_log"] = "no_token_data"
            elif day not in p.tokens:
                u["flow_log"] = "no_observation"
            elif prev_day not in p.tokens:
                u["flow_log"] = "previous_day_missing"
            else:
                flow = fixed_price_flow(p.tokens[day], p.tokens[prev_day])
                f.flow_coverage, f.flow_removed_share, f.flow_added_share = flow["coverage"], flow["removed_share"], flow["added_share"]
                f.flow_unpriced_assets = flow["unpriced"]
                if flow["base_usd"] < min_base_usd:
                    u["flow_log"] = "below_min_base"
                elif flow["coverage"] is None or flow["coverage"] < MIN_FLOW_COVERAGE:
                    u["flow_log"] = "asset_set_changed"
                elif flow["flow_log"] is None:
                    u["flow_log"] = "unavailable"
                else:
                    f.flow_log = flow["flow_log"]
                    f.flow_contributions = flow["contributions"]
                    flows[day] = f.flow_log
            if f.flow_log is not None:
                f.flow_z, reason = robust_z(f.flow_log, _window(flows, day, BASELINE_DAYS, 1))
                if reason:
                    u["flow_z"] = reason
                if f.tvl_log_return is not None:
                    f.non_flow_effect = f.tvl_log_return - f.flow_log
            else:
                u["flow_z"] = u.get("flow_log", "unavailable")

            # Lending utilisation
            if not p.borrowed:
                u["utilisation"] = "not_applicable"
            else:
                f.borrowed_usd = p.borrowed.get(day)
                b, liquidity = f.borrowed_usd, p.tvl.get(day)
                if _positive(b) and _positive(liquidity):
                    f.utilisation = b / (b + liquidity)
                    util[day] = f.utilisation
                else:
                    u["utilisation"] = "no_observation"
                prev_b = p.borrowed.get(prev_day)
                if _positive(prev_b) and prev_b >= min_base_usd:
                    f.borrowed_log_change = _log_ratio(b, prev_b)
                if f.borrowed_log_change is None:
                    u["borrowed_log_change"] = ("no_observation" if b is None else "previous_day_missing" if prev_b is None
                                                else "below_min_base")
                if f.utilisation is not None and prev_day in util:
                    f.utilisation_change = f.utilisation - util[prev_day]
                    util_change[day] = f.utilisation_change
                    f.utilisation_change_z, reason = robust_z(f.utilisation_change, _window(util_change, day, BASELINE_DAYS, 1))
                    if reason:
                        u["utilisation_change_z"] = reason
                else:
                    u["utilisation_change"] = u.get("utilisation", "previous_day_missing")
                    u["utilisation_change_z"] = u["utilisation_change"]
            out.append(f)
        results[slug] = out
    return results


# --- adapting stored rows (pure; the caller runs the query) -------------------

def inputs_from_rows(protocol_rows: Iterable[Mapping], observation_rows: Iterable[Mapping]) -> dict[str, ProtocolInputs]:
    """Build inputs from rows shaped like ``{slug, category}`` and
    ``{slug, metric, day, symbol, value}`` (``tvl.usd.daily``, ``tokens.qty.daily``,
    ``tokens.usd.daily``, ``tvl.borrowed.usd.daily``). Values are taken as given."""
    protocols = {r["slug"]: ProtocolInputs(slug=r["slug"], category=r["category"]) for r in protocol_rows}
    qty: dict[str, dict[date, dict[str, float]]] = {}
    usd: dict[str, dict[date, dict[str, float]]] = {}
    for r in observation_rows:
        p = protocols.get(r["slug"])
        if p is None or r["value"] is None:
            continue
        value, day = float(r["value"]), r["day"]
        if r["metric"] == "tvl.usd.daily":
            p.tvl[day] = value
        elif r["metric"] == "tvl.borrowed.usd.daily":
            p.borrowed[day] = value
        elif r["metric"] == "tokens.qty.daily":
            qty.setdefault(p.slug, {}).setdefault(day, {})[r["symbol"]] = value
        elif r["metric"] == "tokens.usd.daily":
            usd.setdefault(p.slug, {}).setdefault(day, {})[r["symbol"]] = value
    for slug, days in qty.items():
        for day, assets in days.items():
            prices = usd.get(slug, {}).get(day, {})
            protocols[slug].tokens[day] = {s: AssetDay(q, prices.get(s)) for s, q in assets.items()}
    return protocols
