"""Explanation of a protocol's daily TVL change (``flow_context``), pure and deterministic.

A thin presentation layer over ``features-daily-v1``: it calls
``inputs_from_rows`` and ``compute_daily_features`` unchanged and reshapes the
``DailyFeatures`` of one protocol on one day. No formula is re-implemented
here and nothing is written anywhere.

Meaning of the fields (see ``app.ml.features_daily`` for the calculations):

* ``flow``: estimated change in token *quantities* valued at the previous
  day's DefiLlama USD prices (``flow_log``). It is an estimate, not proof of
  deposits or withdrawals, and needs the previous day, a matched base of at
  least USD 10M and >= 90% matched-value coverage.
* ``non_flow_effect`` = ``tvl_log_return - flow_log``: price moves *plus*
  added, removed and unpriced assets. Never a pure price impact.
* ``contributions``: per-asset shares of the previous day's matched USD value
  (not log returns). Only the ten largest by absolute value are kept by the
  feature module; ``other_assets = flow_total - listed_total`` with
  ``flow_total = expm1(flow_log)`` keeps the remainder visible.
* ``lending``: ``utilisation = borrowed / (borrowed + available liquidity)``;
  Kamino's stored TVL is available liquidity, not gross deposits.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Mapping

from app.ml import features_daily as fd

FLOW_CONTEXT_DAYS = 120  # calendar days of token rows: >= 90-day baselines + the preceding day, with margin
SOURCE = "defillama"
ADAPTERS = ("defillama_tvl_history@1", "defillama_token_history@1")
METRICS = ("tvl.usd.daily", "tokens.qty.daily", "tokens.usd.daily", "tvl.borrowed.usd.daily")
DISCLAIMER = (
    "Estimated from DefiLlama token quantities valued at the previous day's DefiLlama prices. "
    "It is not direct evidence of user deposits or withdrawals. The other (non-flow) effect "
    "includes price moves and assets that were added, removed or unpriced; it is not a pure price impact."
)

# Feature fields whose unavailability is reported in the explanation (excess-return
# and drawdown features use different peers from tvl-robust-v1 and are not exposed).
EXPLAINED_FIELDS = ("tvl_log_return", "flow_log", "flow_z", "borrowed_log_change",
                    "utilisation", "utilisation_change", "utilisation_change_z")

REASON_TEXT = {
    "no_token_data": "DefiLlama publishes no per-asset token history for this protocol.",
    "no_observation": "No observation is stored for this day.",
    "previous_day_missing": "The previous day has no observation, so a daily change cannot be measured.",
    "below_min_base": f"The previous-day base is below USD {fd.MIN_BASE_USD / 1e6:.0f}M, too small for a meaningful percentage change.",
    "asset_set_changed": f"Assets present on both days cover less than {fd.MIN_FLOW_COVERAGE:.0%} of the previous day's value.",
    "insufficient_baseline": f"Fewer than {fd.MIN_BASELINE_POINTS} earlier values in the trailing {fd.BASELINE_DAYS}-day window.",
    "zero_dispersion": "The trailing baseline has no variation, so a robust z-score is undefined.",
    "regime_break": "The TVL series changed meaning (residual balance after a regime break) and is not comparable.",
    "not_applicable": "No borrowed-value history exists for this protocol.",
    "unavailable": "The value could not be computed from the stored observations.",
}


def _round(v: float | None, digits: int = 6) -> float | None:
    return None if v is None else round(v, digits) + 0.0  # + 0.0 turns -0.0 into 0.0


def _unavailable(f: fd.DailyFeatures) -> list[dict]:
    return [{"field": name, "code": f.unavailable[name], "detail": REASON_TEXT.get(f.unavailable[name], f.unavailable[name])}
            for name in EXPLAINED_FIELDS if name in f.unavailable]


def _contributions(f: fd.DailyFeatures) -> dict | None:
    if f.flow_log is None or not f.flow_contributions:
        return None
    listed_total = sum(share for _, share in f.flow_contributions)
    flow_total = math.expm1(f.flow_log)
    return {
        "unit": "share of the previous day's matched USD value (simple, not log)",
        "positive": [{"symbol": s, "share": _round(v)} for s, v in f.flow_contributions if v > 0],
        "negative": [{"symbol": s, "share": _round(v)} for s, v in f.flow_contributions if v < 0],
        "unchanged": [s for s, v in f.flow_contributions if v == 0],
        "listed_total": _round(listed_total),
        "flow_total": _round(flow_total),
        "other_assets": _round(flow_total - listed_total),
        "max_listed": fd.MAX_CONTRIBUTIONS,
    }


def _empty(measurement_day: date | None, code: str, regime: str | None = None) -> dict:
    return {
        "feature_version": fd.FEATURE_VERSION,
        "measurement_day": None if measurement_day is None else measurement_day.isoformat(),
        "previous_day": None if measurement_day is None else (measurement_day - timedelta(days=1)).isoformat(),
        "regime": regime, "available": False, "flow": None, "coverage": None, "non_flow_effect": None,
        "tvl_log_return": None, "sol_log_return": None, "lending": None, "contributions": None,
        "unavailable": [{"field": "flow_log", "code": code, "detail": REASON_TEXT.get(code, code)}],
        "source": {"source": SOURCE, "adapters": list(ADAPTERS), "metrics": list(METRICS)},
        "disclaimer": DISCLAIMER,
    }


def context_from_features(f: fd.DailyFeatures, has_borrowed_history: bool) -> dict:
    """Reshape one protocol-day of ``DailyFeatures``; no values are derived except sums of listed contributions."""
    flow = None
    if f.flow_log is not None:
        flow = {"log_change": _round(f.flow_log), "pct_change": _round(math.expm1(f.flow_log)), "robust_z": _round(f.flow_z, 4),
                "valuation": "previous-day DefiLlama USD prices"}
    coverage = None
    if f.flow_coverage is not None:
        coverage = {"matched_share": _round(f.flow_coverage), "removed_share": _round(f.flow_removed_share),
                    "added_share": _round(f.flow_added_share), "unpriced_assets": f.flow_unpriced_assets,
                    "minimum_matched_share": fd.MIN_FLOW_COVERAGE}
    lending = None
    if has_borrowed_history:
        lending = {"borrowed_usd": _round(f.borrowed_usd, 2), "borrowed_log_change": _round(f.borrowed_log_change),
                   "utilisation": _round(f.utilisation), "utilisation_change": _round(f.utilisation_change),
                   "utilisation_change_z": _round(f.utilisation_change_z, 4),
                   "definition": "borrowed / (borrowed + available liquidity); TVL here is available liquidity, not gross deposits"}
    return {
        "feature_version": fd.FEATURE_VERSION,
        "measurement_day": f.day.isoformat(),
        "previous_day": (f.day - timedelta(days=1)).isoformat(),
        "regime": f.regime,
        "available": f.flow_log is not None,
        "flow": flow,
        "coverage": coverage,
        "non_flow_effect": None if f.non_flow_effect is None else {"log_change": _round(f.non_flow_effect)},
        "tvl_log_return": _round(f.tvl_log_return),
        "sol_log_return": _round(f.sol_log_return),
        "lending": lending,
        "contributions": _contributions(f),
        "unavailable": _unavailable(f),
        "source": {"source": SOURCE, "adapters": list(ADAPTERS), "metrics": list(METRICS)},
        "disclaimer": DISCLAIMER,
    }


def build_flow_context(protocol_rows: Iterable[Mapping], observation_rows: Iterable[Mapping], slug: str,
                       measurement_day: date | None, *, regime_breaks: Mapping[str, date] = fd.REGIME_BREAKS) -> dict:
    """Explanation for ``slug`` on ``measurement_day`` (the baseline's latest closed day).

    ``protocol_rows``: ``{slug, category}``; ``observation_rows``: ``{slug, metric, day, symbol, value}``
    as accepted by ``features_daily.inputs_from_rows``. Rows dated after ``measurement_day`` are ignored,
    so the explanation can never use later data.
    """
    if measurement_day is None:
        return _empty(None, "no_observation")
    rows = [r for r in observation_rows if r["day"] <= measurement_day]
    inputs = fd.inputs_from_rows(protocol_rows, rows)
    if slug not in inputs:
        return _empty(measurement_day, "no_observation")
    features = fd.compute_daily_features(inputs, regime_breaks=regime_breaks)
    day = next((f for f in features.get(slug, []) if f.day == measurement_day), None)
    if day is None:
        brk = regime_breaks.get(slug)
        return _empty(measurement_day, "no_observation", "residual" if brk and measurement_day >= brk else "normal")
    return context_from_features(day, bool(inputs[slug].borrowed))
