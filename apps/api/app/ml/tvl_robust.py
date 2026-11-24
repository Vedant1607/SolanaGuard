"""``tvl-robust-v1``: robust daily TVL behaviour scoring (pure NumPy).

Input: closed daily TVL observations per protocol (DefiLlama history). For
each protocol ``p`` and day ``d``:

* ``r[p,d] = ln(TVL[p,d] / TVL[p,d-1])``, defined only when day ``d-1`` was
  observed. Missing days are never filled, so a gap leaves the next day's
  return undefined.
* ``peer[p,d]``: median of the *other* protocols' returns on ``d`` (at least
  ``MIN_MARKET_PEERS``); leaving ``p`` out keeps a protocol from dragging its
  own market reference.
* ``excess[p,d] = r[p,d] - peer[p,d]``: the protocol-specific part of the move.
* ``z[p,d] = (excess[p,d] - median(B)) / scale(B)`` where ``B`` is the excess
  over the strictly preceding ``BASELINE_DAYS`` calendar days; ``scale`` is
  ``1.4826 * MAD``, falling back to ``1.2533 * mean absolute deviation`` when
  the MAD is zero; with no dispersion at all the day is not scored.
* ``market_z[p,d]``: the same robust z for ``peer[p,d]`` against its own
  trailing window, so a market-wide move is reported separately from a
  protocol-specific one.
* ``drawdown[p,d] = TVL[p,d] / max(TVL over the preceding DRAWDOWN_DAYS) - 1``.

Every baseline is strictly trailing: day ``d`` never sees day ``d`` or later in
its own baseline, so appending future days never changes a past score.

A score is *unusualness relative to this protocol's own history and to its
peers*. It is not a probability of an exploit, compromise or loss.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

MODEL_NAME = "tvl-robust"
MODEL_VERSION = "tvl-robust-v1"
METHOD = "trailing 90-day median/MAD robust z-score of peer-adjusted daily log returns"

BASELINE_DAYS = 90
MIN_HISTORY_DAYS = 90  # closed observations strictly before the scored day
MIN_BASELINE_RETURNS = 60  # excess returns available inside the baseline window
DRAWDOWN_DAYS = 30
MIN_DRAWDOWN_POINTS = 20
MIN_MARKET_PEERS = 3
MAX_LATEST_AGE_DAYS = 3  # older history is reported as insufficient, not scored as current
MAD_SCALE = 1.4826
MEAN_AD_SCALE = math.sqrt(math.pi / 2)  # 1.2533: mean absolute deviation -> sigma for normal data

# |z| at or above this marks a day UNUSUAL. Frozen from the 2023–2024 tuning
# period by scripts/evaluate_tvl_robust.py (smallest grid value whose
# tuning-period flag rate is <= TARGET_FLAG_RATE); see the evaluation report.
THRESHOLD = 8.0  # selected 2026-11-12: tuning (2023–2024) flag rate 0.86% at 8.0 vs 1.06% at 7.5
TARGET_FLAG_RATE = 0.01

DISCLAIMER = (
    "Unusualness relative to this protocol's own history and to its peers. "
    "It is not a probability of an exploit, compromise or loss."
)


@dataclass(frozen=True)
class DailyObservation:
    day: date
    value: float
    observation_id: str


@dataclass
class DayScore:
    day: date
    value: float
    observation_id: str
    previous_observation_id: str | None = None
    log_return: float | None = None
    peer_return: float | None = None
    peers: int = 0
    excess_return: float | None = None
    robust_z: float | None = None
    baseline_points: int = 0
    baseline_median: float | None = None
    baseline_scale: float | None = None
    scale_method: str | None = None
    market_z: float | None = None
    drawdown: float | None = None
    drawdown_points: int = 0
    history_days: int = 0  # closed observations strictly before this day
    reasons: list[str] = field(default_factory=list)  # why robust_z is None

    @property
    def scored(self) -> bool:
        return self.robust_z is not None


def _robust_z(value: float, baseline: np.ndarray) -> tuple[float | None, float | None, float | None, str | None, str | None]:
    """(z, median, scale, scale_method, reason_if_unscored) against a NaN-free baseline."""
    median = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - median)))
    if mad > 0:
        scale, method = MAD_SCALE * mad, "mad"
    else:
        mean_ad = float(np.mean(np.abs(baseline - median)))
        if mean_ad > 0:
            scale, method = MEAN_AD_SCALE * mean_ad, "mean_absolute_deviation"
        else:
            return None, median, 0.0, None, "zero_dispersion"
    return (value - median) / scale, median, scale, method, None


def clean_series(observations: list[DailyObservation]) -> tuple[list[DailyObservation], dict[str, int]]:
    """Drop invalid values and ambiguous duplicate days; never fill."""
    dropped: dict[str, int] = {}
    by_day: dict[date, list[DailyObservation]] = {}
    for obs in observations:
        if not (isinstance(obs.value, (int, float)) and math.isfinite(obs.value) and obs.value > 0):
            dropped["invalid_value"] = dropped.get("invalid_value", 0) + 1
            continue
        by_day.setdefault(obs.day, []).append(obs)
    kept = []
    for day in sorted(by_day):
        if len(by_day[day]) > 1:
            dropped["duplicate_day"] = dropped.get("duplicate_day", 0) + len(by_day[day])
            continue
        kept.append(by_day[day][0])
    return kept, dropped


def compute_scores(series: dict[str, list[DailyObservation]]) -> dict[str, list[DayScore]]:
    """Score every closed day of every protocol. Protocols are peers of each other."""
    cleaned = {slug: clean_series(obs)[0] for slug, obs in series.items()}
    slugs = sorted(slug for slug, obs in cleaned.items() if obs)
    if not slugs:
        return {slug: [] for slug in series}
    start = min(cleaned[s][0].day for s in slugs)
    end = max(cleaned[s][-1].day for s in slugs)
    length = (end - start).days + 1
    index = {slug: i for i, slug in enumerate(slugs)}

    values = np.full((len(slugs), length), np.nan)
    for slug in slugs:
        for obs in cleaned[slug]:
            values[index[slug], (obs.day - start).days] = obs.value

    returns = np.full_like(values, np.nan)
    returns[:, 1:] = np.log(values[:, 1:] / values[:, :-1])  # NaN whenever either day is missing

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN medians are expected
        peer = np.full_like(values, np.nan)
        peer_counts = np.zeros_like(values, dtype=int)
        for i in range(len(slugs)):
            others = np.delete(returns, i, axis=0)
            counts = np.sum(~np.isnan(others), axis=0)
            med = np.nanmedian(others, axis=0) if others.size else np.full(length, np.nan)
            peer[i] = np.where(counts >= MIN_MARKET_PEERS, med, np.nan)
            peer_counts[i] = counts
    excess = returns - peer

    results: dict[str, list[DayScore]] = {slug: [] for slug in series}
    for slug in slugs:
        i = index[slug]
        observed = ~np.isnan(values[i])
        history = np.cumsum(observed) - observed  # observations strictly before each day
        by_offset = {(o.day - start).days: o for o in cleaned[slug]}
        for t in np.flatnonzero(observed):
            obs = by_offset[int(t)]
            score = DayScore(day=obs.day, value=obs.value, observation_id=obs.observation_id, history_days=int(history[t]))
            prev = by_offset.get(int(t) - 1)
            score.previous_observation_id = prev.observation_id if prev else None

            if not np.isnan(returns[i, t]):
                score.log_return = float(returns[i, t])
            score.peers = int(peer_counts[i, t])
            if not np.isnan(peer[i, t]):
                score.peer_return = float(peer[i, t])

            lo = max(0, int(t) - DRAWDOWN_DAYS)
            window = values[i, lo:t]
            window = window[~np.isnan(window)]
            score.drawdown_points = int(window.size)
            if window.size >= MIN_DRAWDOWN_POINTS:
                score.drawdown = float(obs.value / window.max() - 1.0)

            lo = max(0, int(t) - BASELINE_DAYS)
            peer_base = peer[i, lo:t]
            peer_base = peer_base[~np.isnan(peer_base)]
            if score.peer_return is not None and peer_base.size >= MIN_BASELINE_RETURNS:
                score.market_z = _robust_z(score.peer_return, peer_base)[0]

            if score.history_days < MIN_HISTORY_DAYS:
                score.reasons.append("insufficient_history")
            if score.log_return is None:
                score.reasons.append("previous_day_missing")
            if score.peer_return is None:
                score.reasons.append("insufficient_peers")
            if score.reasons:
                results[slug].append(score)
                continue

            score.excess_return = float(excess[i, t])
            base = excess[i, lo:t]
            base = base[~np.isnan(base)]
            score.baseline_points = int(base.size)
            if base.size < MIN_BASELINE_RETURNS:
                score.reasons.append("insufficient_baseline")
                results[slug].append(score)
                continue
            z, median, scale, method, reason = _robust_z(score.excess_return, base)
            score.baseline_median, score.baseline_scale, score.scale_method = median, scale, method
            if reason:
                score.reasons.append(reason)
            else:
                score.robust_z = float(z)
            results[slug].append(score)
    return results


REASON_TEXT = {
    "no_observations": "No closed daily TVL observations are stored for this protocol.",
    "insufficient_history": f"Fewer than {MIN_HISTORY_DAYS} closed daily observations precede the latest day.",
    "previous_day_missing": "The day before the latest closed day is missing, so its daily change is undefined (gaps are not filled).",
    "insufficient_peers": f"Fewer than {MIN_MARKET_PEERS} peer protocols have a return for that day, so the market-adjusted change is undefined.",
    "insufficient_baseline": f"Fewer than {MIN_BASELINE_RETURNS} peer-adjusted returns fall in the trailing {BASELINE_DAYS}-day baseline.",
    "zero_dispersion": "The trailing baseline has no dispersion, so a robust z-score is undefined.",
    "stale_history": f"The latest closed observation is more than {MAX_LATEST_AGE_DAYS} days old; re-run the DefiLlama backfill.",
}


def classify(score: DayScore | None, today: date, threshold: float = THRESHOLD) -> tuple[str, list[str]]:
    """(NORMAL | UNUSUAL | INSUFFICIENT_HISTORY, reason codes) for the latest closed day."""
    if score is None:
        return "INSUFFICIENT_HISTORY", ["no_observations"]
    reasons = list(score.reasons)
    if (today - score.day) > timedelta(days=MAX_LATEST_AGE_DAYS):
        reasons.append("stale_history")
    if reasons or score.robust_z is None:
        return "INSUFFICIENT_HISTORY", reasons or ["insufficient_baseline"]
    return ("UNUSUAL" if abs(score.robust_z) >= threshold else "NORMAL"), []


def explain(score: DayScore, threshold: float = THRESHOLD) -> list[str]:
    """Plain-language contributions of each feature for a scored day."""
    lines: list[str] = []
    if score.log_return is not None and score.peer_return is not None and score.excess_return is not None:
        lines.append(
            f"Daily TVL change {math.expm1(score.log_return):+.2%} versus a peer-median change of "
            f"{math.expm1(score.peer_return):+.2%} across {score.peers} peer protocols: "
            f"a protocol-specific (excess) log return of {score.excess_return:+.4f}."
        )
    if score.robust_z is not None:
        direction = "decline" if score.robust_z < 0 else "increase"
        lines.append(
            f"That excess return is {score.robust_z:+.2f} robust standard deviations from its trailing "
            f"{BASELINE_DAYS}-day median ({score.baseline_points} days, {score.scale_method}); "
            f"|z| >= {threshold:g} is unusual. Direction: {direction}."
        )
    if score.market_z is not None:
        if abs(score.market_z) >= threshold:
            lines.append(f"Peer protocols as a whole also moved unusually (market z {score.market_z:+.2f}): a market-wide day.")
        else:
            lines.append(f"Peer protocols moved within their normal range (market z {score.market_z:+.2f}), so any unusual move is protocol-specific.")
    if score.drawdown is not None:
        if score.drawdown < 0:
            lines.append(f"TVL is {abs(score.drawdown):.1%} below its preceding {DRAWDOWN_DAYS}-day high.")
        else:
            lines.append(f"TVL is at or above its preceding {DRAWDOWN_DAYS}-day high ({score.drawdown:+.1%}).")
    return lines
