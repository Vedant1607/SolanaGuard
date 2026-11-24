"""Offline, time-split evaluation of ``tvl-robust-v1`` on stored DefiLlama history.

Usage (from apps/api, DATABASE_URL set):

    uv run python scripts/evaluate_tvl_robust.py --output tvl_robust_evaluation.json

* Reads closed daily TVL observations inside a READ ONLY transaction; writes
  nothing to the database. The only output is the JSON report.
* Tuning: on days in 2023-01-01..2024-12-31, choose the smallest threshold k
  on a fixed grid whose protocol-day flag rate is <= TARGET_FLAG_RATE.
* The threshold is then frozen and applied unchanged to the holdout
  (2025-01-01 onwards). Earlier days are reported as "pre_tuning".
* A flag is a scored day with |z| >= k. It is labelled market-wide when the
  peer median itself moved unusually that day (|market_z| >= k), otherwise
  protocol-specific. Market-wide days are also counted on their own.
* No incident labels are used: no sourced list of documented events is
  available in this repository, and none is invented.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import asyncpg  # noqa: E402

from app.adapters.defillama_history import ADAPTER_KEY, METRIC_TVL_DAILY  # noqa: E402
from app.config import settings  # noqa: E402
from app.ml import tvl_robust as model  # noqa: E402

TUNING = (date(2023, 1, 1), date(2024, 12, 31))
HOLDOUT_START = date(2025, 1, 1)
THRESHOLD_GRID = [x / 2 for x in range(6, 25)]  # 3.0 .. 12.0

SERIES_QUERY = """
    SELECT o.id, p.slug, o."sourceObservedAt" AS observed_at, o."valueNumeric" AS value
    FROM metric_observations o
    JOIN protocols p ON p.id = o."protocolId"
    WHERE o."adapterKey" = $1 AND o."metricKey" = $2 AND o.status = 'OK' AND p."isActive" = true
    ORDER BY p.slug, o."sourceObservedAt", o.id
"""


def split_of(day: date) -> str:
    if day < TUNING[0]:
        return "pre_tuning"
    if day <= TUNING[1]:
        return "tuning"
    return "holdout"


def tune_threshold(scores: dict[str, list[model.DayScore]]) -> tuple[float, list[dict]]:
    tuning = [s.robust_z for rows in scores.values() for s in rows if s.scored and TUNING[0] <= s.day <= TUNING[1]]
    curve = []
    chosen = None
    for k in THRESHOLD_GRID:
        rate = sum(abs(z) >= k for z in tuning) / len(tuning) if tuning else None
        curve.append({"threshold": k, "tuning_flag_rate": rate})
        if chosen is None and rate is not None and rate <= model.TARGET_FLAG_RATE:
            chosen = k
    return (chosen if chosen is not None else THRESHOLD_GRID[-1]), curve


def evaluate(series: dict[str, list[model.DailyObservation]]) -> dict:
    scores = model.compute_scores(series)
    threshold, curve = tune_threshold(scores)

    coverage = {}
    per_year: dict[str, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    per_split: dict[str, Counter] = defaultdict(Counter)
    market_days: dict[str, set[date]] = defaultdict(set)
    holdout_flags = []

    for slug, rows in sorted(scores.items()):
        days = [s.day for s in rows]
        span = (days[-1] - days[0]).days + 1 if days else 0
        unscored = Counter(reason for s in rows for reason in s.reasons)
        coverage[slug] = {
            "first_day": days[0].isoformat() if days else None,
            "last_day": days[-1].isoformat() if days else None,
            "closed_days": len(days),
            "missing_dates_within_span": span - len(days),
            "scored_days": sum(s.scored for s in rows),
            "unscored_reasons": dict(unscored),
        }
        for s in rows:
            year, split = s.day.year, split_of(s.day)
            market_wide = s.market_z is not None and abs(s.market_z) >= threshold
            if market_wide:
                market_days[split].add(s.day)
            if not s.scored:
                continue
            bucket = per_year[slug][year]
            bucket["scored_days"] += 1
            per_split[split]["scored_days"] += 1
            if abs(s.robust_z) >= threshold:
                kind = "market_wide_flags" if market_wide else "protocol_specific_flags"
                bucket["flags"] += 1
                bucket[kind] += 1
                bucket["decline_flags" if s.robust_z < 0 else "increase_flags"] += 1
                per_split[split]["flags"] += 1
                per_split[split][kind] += 1
                if split == "holdout":
                    holdout_flags.append({
                        "protocol": slug, "day": s.day.isoformat(), "robust_z": round(s.robust_z, 2),
                        "daily_change": round(float(math.expm1(s.log_return)), 4),
                        "peer_median_change": round(float(math.expm1(s.peer_return)), 4),
                        "market_z": None if s.market_z is None else round(s.market_z, 2),
                        "drawdown_30d": None if s.drawdown is None else round(s.drawdown, 4),
                        "market_wide": market_wide, "observation_id": s.observation_id,
                    })

    def rates(counter: Counter) -> dict:
        out = dict(counter)
        out["flag_rate"] = counter["flags"] / counter["scored_days"] if counter["scored_days"] else None
        return out

    return {
        "model": {"name": model.MODEL_NAME, "version": model.MODEL_VERSION, "method": model.METHOD,
                  "baseline_days": model.BASELINE_DAYS, "min_history_days": model.MIN_HISTORY_DAYS,
                  "min_baseline_returns": model.MIN_BASELINE_RETURNS, "min_market_peers": model.MIN_MARKET_PEERS},
        "data": {"source": "defillama", "adapter": ADAPTER_KEY, "metric_key": METRIC_TVL_DAILY,
                 "note": "Real stored DefiLlama daily TVL observations; no synthetic data."},
        "splits": {"tuning": [TUNING[0].isoformat(), TUNING[1].isoformat()], "holdout_start": HOLDOUT_START.isoformat()},
        "threshold": {"selected": threshold, "target_flag_rate": model.TARGET_FLAG_RATE,
                      "frozen_before_holdout": True, "code_threshold": model.THRESHOLD,
                      "code_matches_selected": model.THRESHOLD == threshold, "tuning_curve": curve},
        "coverage": coverage,
        "by_split": {split: rates(c) for split, c in per_split.items()},
        "market_wide_days": {split: len(d) for split, d in market_days.items()},
        "by_protocol_and_year": {slug: {str(y): rates(c) for y, c in sorted(years.items())} for slug, years in per_year.items()},
        "holdout_flags": sorted(holdout_flags, key=lambda f: (f["day"], f["protocol"])),
        "documented_events": [],
        "documented_events_note": "No sourced incident list is available; recall against real events is not measured.",
        "disclaimer": model.DISCLAIMER,
    }


async def load_series(dsn: str) -> dict[str, list[model.DailyObservation]]:
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(SERIES_QUERY, ADAPTER_KEY, METRIC_TVL_DAILY)
    finally:
        await conn.close()
    series: dict[str, list[model.DailyObservation]] = defaultdict(list)
    for row in rows:
        series[row["slug"]].append(model.DailyObservation(day=row["observed_at"].date(), value=float(row["value"]), observation_id=row["id"]))
    return dict(series)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline time-split evaluation of tvl-robust-v1")
    parser.add_argument("--output", required=True, help="path of the JSON report to write")
    args = parser.parse_args()
    series = asyncio.run(load_series(settings.DATABASE_URL))
    report = evaluate(series)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    t = report["threshold"]
    print(f"threshold selected={t['selected']} (code {t['code_threshold']}, match={t['code_matches_selected']})")
    for split, r in sorted(report["by_split"].items()):
        print(f"{split:10} scored={r.get('scored_days', 0):6} flags={r.get('flags', 0):4} rate={r['flag_rate']}")
    print(f"report written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
