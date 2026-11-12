"""Offline comparison of daily anomaly detectors (``multivariate-eval-v1``).

Usage (from apps/api, DATABASE_URL set; writes only the two report files):

    uv run python scripts/evaluate_multivariate.py --output-dir /path/outside/the/repo

Compares, on stored DefiLlama history, with no database writes:

A. ``tvl-robust-v1`` exactly as implemented (``compute_scores``, THRESHOLD 8.0).
B. ``transparent-maxz``: the largest absolute robust z among the *material*
   per-feature signals of ``features-daily-v1``. A signal is material only if
   its absolute size reaches a gate (the 95th percentile of that signal's
   absolute size on training days, pooled across protocols):
     * ``excess``      |excess_return|         log return  (not used for liquid staking when flow exists)
     * ``flow``        |flow_log|              log change of holdings at fixed prices
     * ``utilisation`` |utilisation_change|    absolute utilisation points (0-1 scale)
   Drawdown and the volatility ratio are reported as context, not triggers:
   they are not z-scores and would double-count the same move.
C. Isolation Forests on complete feature vectors (no imputation), one model per
   distinct feature schema, for three feature sets (C1 core TVL, C2 + flow,
   C3 + utilisation). A protocol uses the richest schema it is *capable* of;
   a day missing any feature of that schema is unscored, never filled.

Chronology: training <= 2024-06-30; calibration 2024-07-01..2024-12-31;
holdout >= 2025-01-01. Every detector's decision threshold is set on the
calibration split to the baseline's own calibration flag rate, so all models
are compared at the same alert budget. Holdout is never used for tuning.
Features are strictly trailing (tested in ``test_features_daily``), so
computing them once over the full history equals computing them day by day.
Monthly walk-forward refits fit on days before ``month - 184 days`` and
calibrate on the 184 days before the month: only data before the month is used.

There are no incident labels: precision and recall cannot be measured, and the
case studies are qualitative checks against the source evidence gathered in
the Phase 3A.1 audit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import numpy as np  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402

from app.ml import features_daily as fd  # noqa: E402
from app.ml import tvl_robust  # noqa: E402

EVAL_VERSION = "multivariate-eval-v1"
TRAIN_END = date(2024, 6, 30)
CALIB_START, CALIB_END = date(2024, 7, 1), date(2024, 12, 31)
HOLDOUT_START = date(2025, 1, 1)
WALK_CALIB_DAYS = 184  # length of the trailing calibration window in walk-forward refits (= 2024-H2)

GATE_QUANTILE = 0.95
MIN_GATE_ROWS = 100
TRANSPARENT_GRID = [x / 2 for x in range(6, 81)]  # 3.0 .. 40.0
MIN_TRAIN_ROWS = 250
MIN_CALIB_ROWS = 100
IF_TREES = 300
PRIMARY_SEED = 11
SEEDS = (11, 22, 33, 44, 55)
MARKET_CONTEXT_Z = 5.0  # context label only: peer-median or SOL move with |robust z| >= 5

# --- rows --------------------------------------------------------------------


def split_of(day: date) -> str:
    if day <= TRAIN_END:
        return "train"
    if day <= CALIB_END:
        return "calibration"
    return "holdout"


@dataclass
class Row:
    slug: str
    day: date
    category: str
    f: fd.DailyFeatures
    baseline_z: float | None
    baseline_market_z: float | None
    sol_z: float | None
    has_flow_capability: bool
    has_borrow_capability: bool

    @property
    def key(self) -> tuple[str, date]:
        return self.slug, self.day

    @property
    def split(self) -> str:
        return split_of(self.day)

    @property
    def is_lst(self) -> bool:
        return self.category == fd.LIQUID_STAKING

    @property
    def market_context(self) -> bool:
        return any(z is not None and abs(z) >= MARKET_CONTEXT_Z for z in (self.baseline_market_z, self.sol_z))


def build_rows(features: dict[str, list[fd.DailyFeatures]], baseline: dict[str, list[tvl_robust.DayScore]],
               protocols: dict[str, fd.ProtocolInputs]) -> list[Row]:
    sol = {f.day: f.sol_log_return for fs in features.values() for f in fs if f.sol_log_return is not None}
    sol_z = {}
    for day in sorted(sol):
        z, _ = fd.robust_z(sol[day], [sol.get(day - timedelta(days=k)) for k in range(1, fd.BASELINE_DAYS + 1)])
        sol_z[day] = z
    base = {(slug, s.day): s for slug, scores in baseline.items() for s in scores}
    rows = []
    for slug in sorted(features):
        p = protocols[slug]
        for f in features[slug]:
            b = base.get((slug, f.day))
            rows.append(Row(slug, f.day, p.category, f, None if b is None else b.robust_z, None if b is None else b.market_z,
                            sol_z.get(f.day), bool(p.tokens), bool(p.borrowed)))
    rows.sort(key=lambda r: (r.day, r.slug))
    return rows


def attribution(row: Row, flow_gate: float | None) -> str:
    """Whether a flagged day moved holdings materially (quantity) or not (price or other)."""
    if row.f.flow_log is None or flow_gate is None:
        return "no_flow_data"
    return "quantity" if abs(row.f.flow_log) >= flow_gate else "price_or_other"


@dataclass
class Result:
    scored: bool
    flagged: bool = False
    score: float | None = None
    reason: str | None = None  # why unscored
    explanation: list | None = None


# --- A: baseline -------------------------------------------------------------


class Baseline:
    name = "A_tvl_robust_v1"

    def fit(self, train, calib, target_rate):
        return self

    @property
    def threshold(self) -> float:
        return tvl_robust.THRESHOLD

    def score(self, row: Row) -> Result:
        if row.baseline_z is None:
            return Result(False, reason="unscored_by_baseline")
        return Result(True, abs(row.baseline_z) >= tvl_robust.THRESHOLD, abs(row.baseline_z),
                      explanation=[{"feature": "tvl_robust_z", "value": round(row.baseline_z, 3)}])

    def score_rows(self, rows: list[Row]) -> dict:
        return {r.key: self.score(r) for r in rows}

    def config(self) -> dict:
        return {"model": tvl_robust.MODEL_VERSION, "threshold": tvl_robust.THRESHOLD, "method": tvl_robust.METHOD,
                "note": "outputs of compute_scores, unchanged; no refits"}


# --- B: transparent maximum robust z with size gates --------------------------


def signals(row: Row) -> list[tuple[str, float, float]]:
    """(signal, robust z, absolute size). Liquid staking uses flow, or excess only when flow is absent."""
    f, out = row.f, []
    use_excess = not row.is_lst or f.flow_z is None
    if use_excess and f.excess_return_z is not None and f.excess_return is not None:
        out.append(("excess", f.excess_return_z, abs(f.excess_return)))
    if f.flow_z is not None and f.flow_log is not None:
        out.append(("flow", f.flow_z, abs(f.flow_log)))
    if f.utilisation_change_z is not None and f.utilisation_change is not None:
        out.append(("utilisation", f.utilisation_change_z, abs(f.utilisation_change)))
    return out


class TransparentMaxZ:
    name = "B_transparent_maxz"

    def __init__(self):
        self.gates: dict[str, float] = {}
        self.gate_rows: dict[str, int] = {}
        self.threshold: float | None = None
        self.calibration_rows = 0
        self.calibration_rate: float | None = None
        self.status = "unfitted"

    def fit(self, train: list[Row], calib: list[Row], target_rate: float):
        sizes = defaultdict(list)
        for r in train:
            for name, _, size in signals(r):
                sizes[name].append(size)
        for name in sorted(sizes):
            self.gate_rows[name] = len(sizes[name])
            if len(sizes[name]) >= MIN_GATE_ROWS:
                self.gates[name] = float(np.quantile(sizes[name], GATE_QUANTILE))
        scores = [s for s in (self._raw(r)[0] for r in calib) if s is not None]
        self.calibration_rows = len(scores)
        if len(scores) < MIN_CALIB_ROWS:
            self.status = "insufficient_calibration"
            return self
        arr = np.asarray(scores)
        self.threshold = next((k for k in TRANSPARENT_GRID if np.mean(arr >= k) <= target_rate), TRANSPARENT_GRID[-1])
        self.calibration_rate = float(np.mean(arr >= self.threshold))
        self.status = "ok"
        return self

    def _raw(self, row: Row):
        usable = [(n, z, s) for n, z, s in signals(row) if n in self.gates]
        if not usable:
            return None, [], "no_gated_signal"
        detail = [{"signal": n, "z": round(z, 3), "size": round(s, 5), "gate": round(self.gates[n], 5),
                   "material": s >= self.gates[n]} for n, z, s in usable]
        material = [abs(z) for n, z, s in usable if s >= self.gates[n]]
        detail.sort(key=lambda d: (not d["material"], -abs(d["z"])))
        return (max(material) if material else 0.0), detail, None

    def score(self, row: Row) -> Result:
        if self.status != "ok":
            return Result(False, reason=self.status)
        score, detail, reason = self._raw(row)
        if score is None:
            return Result(False, reason=reason)
        return Result(True, score >= self.threshold, score, explanation=detail)

    def score_rows(self, rows: list[Row]) -> dict:
        return {r.key: self.score(r) for r in rows}

    def config(self) -> dict:
        return {"gate_quantile": GATE_QUANTILE, "gates": {k: round(v, 6) for k, v in self.gates.items()},
                "gate_units": {"excess": "abs daily log return", "flow": "abs daily log change of holdings at fixed prices",
                               "utilisation": "abs change in utilisation (0-1 scale)"},
                "gate_training_rows": self.gate_rows, "threshold": self.threshold, "calibration_rows": self.calibration_rows,
                "calibration_flag_rate": self.calibration_rate, "status": self.status}


# --- C: Isolation Forest --------------------------------------------------------

GROUP_FEATURES: dict[str, tuple[str, ...]] = {
    "core": ("excess_return_z", "drawdown_30d", "vol_ratio_7_90"),
    "flow_lst": ("flow_z", "drawdown_30d", "vol_ratio_7_90"),
    "flow": ("excess_return_z", "drawdown_30d", "vol_ratio_7_90", "flow_z"),
    "flow_util": ("excess_return_z", "drawdown_30d", "vol_ratio_7_90", "flow_z", "utilisation_change_z"),
}
FEATURE_SETS: dict[str, tuple[str, ...]] = {  # first group a protocol is capable of
    "C1_iforest_core": ("core",),
    "C2_iforest_tvl_flow": ("flow_lst", "flow", "core"),
    "C3_iforest_tvl_flow_util": ("flow_util", "flow_lst", "flow", "core"),
}


def capable(group: str, row: Row) -> bool:
    if group == "core":
        return True
    if group == "flow_lst":
        return row.is_lst and row.has_flow_capability
    if group == "flow":
        return not row.is_lst and row.has_flow_capability
    if group == "flow_util":
        return row.has_borrow_capability and row.has_flow_capability
    raise KeyError(group)


def transform(name: str, value: float) -> float:
    """Fixed, parameter-free transforms for heavy tails (no fitted statistics)."""
    if name.endswith("_z"):
        return math.copysign(math.log1p(abs(value)), value)
    if name == "vol_ratio_7_90":
        return math.log1p(value)
    return value  # drawdown_30d is bounded


def vector(row: Row, features: tuple[str, ...]) -> list[float] | None:
    values = [getattr(row.f, n) for n in features]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    return [transform(n, v) for n, v in zip(features, values)]


class IFGroup:
    def __init__(self, group: str, seed: int):
        self.group, self.features, self.seed = group, GROUP_FEATURES[group], seed
        self.model = None
        self.threshold = None
        self.status = "unfitted"
        self.train_rows = self.calib_rows = 0
        self.calibration_rate = None
        self.train_by_protocol: Counter = Counter()
        self.reference: np.ndarray | None = None

    def fit(self, train: list[Row], calib: list[Row], target_rate: float):
        tx = [(r, v) for r in train if (v := vector(r, self.features)) is not None]
        cx = [v for r in calib if (v := vector(r, self.features)) is not None]
        self.train_rows, self.calib_rows = len(tx), len(cx)
        self.train_by_protocol = Counter(r.slug for r, _ in tx)
        if len(tx) < MIN_TRAIN_ROWS:
            self.status = "insufficient_training"
            return self
        if len(cx) < MIN_CALIB_ROWS:
            self.status = "insufficient_calibration"
            return self
        x = np.asarray([v for _, v in tx])
        self.model = IsolationForest(n_estimators=IF_TREES, max_samples="auto", contamination="auto",
                                     random_state=self.seed, n_jobs=1).fit(x)
        self.reference = np.sort(x, axis=0)
        anomaly = -self.model.score_samples(np.asarray(cx))
        self.threshold = float(np.quantile(anomaly, 1 - target_rate, method="higher"))
        self.calibration_rate = float(np.mean(anomaly >= self.threshold))
        self.status = "ok"
        return self

    def score_rows(self, rows: list[Row]) -> dict:
        out, ready = {}, []
        for r in rows:
            if self.status != "ok":
                out[r.key] = Result(False, reason=f"{self.group}:{self.status}")
                continue
            v = vector(r, self.features)
            if v is None:
                missing = [n for n in self.features if getattr(r.f, n) is None]
                out[r.key] = Result(False, reason=f"{self.group}:incomplete_features:{','.join(missing)}")
            else:
                ready.append((r, v))
        if ready:
            x = np.asarray([v for _, v in ready])
            anomaly = -self.model.score_samples(x)
            pct = np.column_stack([np.searchsorted(self.reference[:, i], x[:, i], side="right") / len(self.reference)
                                   for i in range(len(self.features))])
            for j, (r, _) in enumerate(ready):
                explanation = [{"feature": n, "value": round(getattr(r.f, n), 4), "training_percentile": round(float(pct[j, i]), 4)}
                               for i, n in enumerate(self.features)]
                explanation.sort(key=lambda e: -abs(e["training_percentile"] - 0.5))
                out[r.key] = Result(True, bool(anomaly[j] >= self.threshold), float(anomaly[j]),
                                    explanation=[{"group": self.group}] + explanation)
        return out

    def config(self) -> dict:
        return {"features": list(self.features), "status": self.status, "train_rows": self.train_rows,
                "train_rows_by_protocol": dict(sorted(self.train_by_protocol.items())), "calibration_rows": self.calib_rows,
                "threshold": self.threshold, "calibration_flag_rate": self.calibration_rate}


class IFComposite:
    def __init__(self, name: str, seed: int = PRIMARY_SEED):
        self.name, self.seed = name, seed
        self.order = FEATURE_SETS[name]
        self.groups = {g: IFGroup(g, seed) for g in self.order}

    def assign(self, row: Row) -> str:
        return next(g for g in self.order if capable(g, row))

    def fit(self, train: list[Row], calib: list[Row], target_rate: float):
        for g, model in self.groups.items():
            model.fit([r for r in train if self.assign(r) == g], [r for r in calib if self.assign(r) == g], target_rate)
        return self

    def score_rows(self, rows: list[Row]) -> dict:
        by_group = defaultdict(list)
        for r in rows:
            by_group[self.assign(r)].append(r)
        out = {}
        for g, members in by_group.items():
            out.update(self.groups[g].score_rows(members))
        return out

    def config(self) -> dict:
        return {"seed": self.seed, "trees": IF_TREES, "max_samples": "auto (min(256, n))", "n_jobs": 1,
                "missing_values": "row unscored (no imputation)", "transforms": "z: sign*log1p|z|; vol ratio: log1p; drawdown: raw",
                "threshold_rule": "anomaly = -score_samples; threshold = calibration quantile at 1 - target rate",
                "groups": {g: m.config() for g, m in self.groups.items()}}


# --- evaluation ----------------------------------------------------------------


def fit_frozen(factory, rows: list[Row], target_rate: float):
    train = [r for r in rows if r.split == "train"]
    calib = [r for r in rows if r.split == "calibration"]
    return factory().fit(train, calib, target_rate)


def score_all(model, rows: list[Row]) -> dict:
    return model.score_rows(rows)


def month_starts(rows: list[Row]) -> list[date]:
    return sorted({date(r.day.year, r.day.month, 1) for r in rows if r.split == "holdout"})


def walk_forward(factory, rows: list[Row], target_rate: float) -> tuple[dict, list[dict]]:
    """Refit monthly using only rows dated before each month."""
    results, stability, previous = {}, [], None
    for start in month_starts(rows):
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        cut = start - timedelta(days=WALK_CALIB_DAYS)
        train = [r for r in rows if r.day < cut]
        calib = [r for r in rows if cut <= r.day < start]
        model = factory().fit(train, calib, target_rate)
        month = [r for r in rows if start <= r.day < end]
        now = model.score_rows(month)
        results.update(now)
        if previous is not None:
            before = previous.score_rows(month)
            a = {k for k, v in now.items() if v.flagged}
            b = {k for k, v in before.items() if v.flagged}
            stability.append({"month": start.isoformat(), "flags_refit": len(a), "flags_previous_model": len(b), "jaccard": jaccard(a, b)})
        previous = model
    return results, stability


def jaccard(a: set, b: set) -> float | None:
    return None if not (a | b) else len(a & b) / len(a | b)


def flagged_keys(results: dict, split: str | None = "holdout") -> set:
    return {k for k, v in results.items() if v.flagged and (split is None or split_of(k[1]) == split)}


def summarize(rows: list[Row], results: dict, flow_gate: float | None) -> dict:
    by_split, by_py = defaultdict(Counter), defaultdict(lambda: defaultdict(Counter))
    unscored = defaultdict(Counter)
    for r in rows:
        res = results[r.key]
        bucket_s, bucket_p = by_split[r.split], by_py[r.slug][r.day.year]
        bucket_s["days"] += 1
        bucket_p["days"] += 1
        if not res.scored:
            unscored[r.slug][res.reason] += 1
            continue
        for b in (bucket_s, bucket_p):
            b["scored"] += 1
            if res.flagged:
                b["flags"] += 1
                b["market_context_flags" if r.market_context else "protocol_specific_flags"] += 1
                b[f"attribution_{attribution(r, flow_gate)}"] += 1

    def rate(c: Counter) -> dict:
        out = dict(sorted(c.items()))
        out["flag_rate"] = round(c["flags"] / c["scored"], 5) if c["scored"] else None
        out["coverage"] = round(c["scored"] / c["days"], 4) if c["days"] else None
        return out

    return {"by_split": {s: rate(c) for s, c in sorted(by_split.items())},
            "by_protocol_and_year": {slug: {str(y): rate(c) for y, c in sorted(years.items())} for slug, years in sorted(by_py.items())},
            "unscored_reasons": {slug: dict(c.most_common()) for slug, c in sorted(unscored.items())}}


def flag_list(rows_by_key: dict, results: dict, flow_gate: float | None, split="holdout") -> list[dict]:
    out = []
    for key in sorted(flagged_keys(results, split), key=lambda k: (k[1], k[0])):
        r, res = rows_by_key[key], results[key]
        out.append({"protocol": r.slug, "day": r.day.isoformat(), "score": round(res.score, 4),
                    "market_context": r.market_context, "attribution": attribution(r, flow_gate),
                    "tvl_log_return": None if r.f.tvl_log_return is None else round(r.f.tvl_log_return, 4),
                    "flow_log": None if r.f.flow_log is None else round(r.f.flow_log, 4),
                    "explanation": res.explanation})
    return out


CASE_STUDIES = [
    ("drift", date(2026, 4, 2), "flag", "Confirmed genuine outflow: ~90% of every asset left Drift Trade; prices moved ~2%."),
    ("drift", date(2026, 6, 30), "no_flag", "Residual balance (~$6M -> $1M) after the April regime break; not protocol-level signal."),
    ("raydium", date(2025, 2, 5), "undetermined", "Unconfirmed: +17.8% spike the day before then -28%; no token breakdown; misrepresented tokens."),
    ("jito", date(2026, 2, 6), "flag_only_if_quantity", "Price-driven: SOL -19.5% then +18.5%; SOL quantity only -1.5%."),
    ("marinade", date(2026, 2, 6), "flag_only_if_quantity", "Mostly price (SOL -19.4%) plus a genuine -5.8% SOL quantity outflow."),
    ("kamino", date(2026, 6, 11), "flag", "Confirmed deposit withdrawal (PYUSD ~-$245M); borrowing flat; utilisation 0.419 -> 0.480."),
]


def consistency(expected: str, res: Result | None, attr: str) -> str:
    if expected == "undetermined":
        return "n/a"
    if res is None or not res.scored:
        return "not_scored" if expected == "flag" else "consistent"
    if expected == "flag":
        return "consistent" if res.flagged else "missed"
    if expected == "no_flag":
        return "inconsistent" if res.flagged else "consistent"
    if not res.flagged:
        return "consistent"
    return "consistent" if attr == "quantity" else "inconsistent"


def case_studies(rows_by_key, models: dict[str, dict], walk: dict[str, dict], flow_gate) -> list[dict]:
    out = []
    for slug, day, expected, evidence in CASE_STUDIES:
        r = rows_by_key.get((slug, day))
        entry = {"protocol": slug, "day": day.isoformat(), "evidence": evidence, "expected": expected, "models": {}}
        if r is not None:
            entry.update({"market_context": r.market_context, "attribution": attribution(r, flow_gate), "regime": r.f.regime,
                          "features": {n: (None if getattr(r.f, n) is None else round(getattr(r.f, n), 4)) for n in
                                       ("tvl_log_return", "excess_return", "excess_return_z", "flow_log", "flow_z",
                                        "non_flow_effect", "utilisation_change", "utilisation_change_z", "drawdown_30d", "vol_ratio_7_90")},
                          "baseline_z": None if r.baseline_z is None else round(r.baseline_z, 3)})
        for name, results in models.items():
            res = results.get((slug, day))
            attr = attribution(r, flow_gate) if r else "no_row"
            entry["models"][name] = {
                "scored": bool(res and res.scored), "flagged": bool(res and res.flagged),
                "score": None if not res or res.score is None else round(res.score, 4),
                "unscored_reason": None if not res or res.scored else res.reason,
                "walk_forward_flagged": None if name not in walk else bool(walk[name].get((slug, day)) and walk[name][(slug, day)].flagged),
                "explanation": None if not res else res.explanation,
                "consistency": consistency(expected, res, attr),
            }
        out.append(entry)
    return out


def coverage(rows: list[Row]) -> dict:
    names = ("excess_return_z", "drawdown_30d", "vol_ratio_7_90", "flow_z", "utilisation_change_z")
    out = defaultdict(dict)
    for slug in sorted({r.slug for r in rows}):
        for split in ("train", "calibration", "holdout"):
            sub = [r for r in rows if r.slug == slug and r.split == split]
            entry = {"days": len(sub), **{n: sum(getattr(r.f, n) is not None for r in sub) for n in names}}
            entry["complete_by_group"] = {g: sum(vector(r, f) is not None for r in sub) for g, f in GROUP_FEATURES.items() if capable(g, sub[0])} if sub else {}
            out[slug][split] = entry
        first = {n: next((r.day.isoformat() for r in rows if r.slug == slug and getattr(r.f, n) is not None), None) for n in names}
        out[slug]["first_available"] = first
    return dict(out)


def evaluate(rows: list[Row]) -> dict:
    rows_by_key = {r.key: r for r in rows}
    baseline = Baseline()
    base_results = score_all(baseline, rows)
    calib_scored = [base_results[r.key] for r in rows if r.split == "calibration" and base_results[r.key].scored]
    target_rate = sum(x.flagged for x in calib_scored) / len(calib_scored)

    transparent = fit_frozen(TransparentMaxZ, rows, target_rate)
    flow_gate = transparent.gates.get("flow")
    models = {baseline.name: base_results, transparent.name: score_all(transparent, rows)}
    configs = {baseline.name: baseline.config(), transparent.name: transparent.config()}
    seed_stability = {}
    for set_name in FEATURE_SETS:
        primary = fit_frozen(lambda: IFComposite(set_name, PRIMARY_SEED), rows, target_rate)
        models[set_name] = score_all(primary, rows)
        configs[set_name] = primary.config()
        per_seed = {}
        for seed in SEEDS:
            m = primary if seed == PRIMARY_SEED else fit_frozen(lambda: IFComposite(set_name, seed), rows, target_rate)
            per_seed[seed] = flagged_keys(models[set_name] if seed == PRIMARY_SEED else score_all(m, rows))
        pairs = [jaccard(per_seed[a], per_seed[b]) for i, a in enumerate(SEEDS) for b in SEEDS[i + 1:]]
        pairs = [p for p in pairs if p is not None]
        seed_stability[set_name] = {"seeds": list(SEEDS), "holdout_flags_per_seed": {str(s): len(v) for s, v in per_seed.items()},
                                    "pairwise_jaccard_mean": round(float(np.mean(pairs)), 4) if pairs else None,
                                    "pairwise_jaccard_min": round(float(np.min(pairs)), 4) if pairs else None,
                                    "flags_in_all_seeds": len(set.intersection(*per_seed.values())) if per_seed else 0}

    walk, walk_stability = {}, {}
    for name, factory in [(transparent.name, TransparentMaxZ)] + [(s, (lambda s=s: IFComposite(s, PRIMARY_SEED))) for s in FEATURE_SETS]:
        walk[name], walk_stability[name] = walk_forward(factory, rows, target_rate)

    base_flags = flagged_keys(base_results)
    comparison = {}
    for name, results in models.items():
        flags = flagged_keys(results)
        scored = {k for k, v in results.items() if v.scored and split_of(k[1]) == "holdout"}
        comparison[name] = {
            "holdout_scored": len(scored), "holdout_flags": len(flags),
            "holdout_flag_rate": round(len(flags) / len(scored), 5) if scored else None,
            "overlap_with_baseline": len(flags & base_flags), "jaccard_with_baseline": jaccard(flags, base_flags),
            "unique_vs_baseline": sorted(f"{s}:{d.isoformat()}" for s, d in flags - base_flags),
            "baseline_flags_not_flagged": sorted(f"{s}:{d.isoformat()}" for s, d in base_flags - flags),
        }
        if name in walk:
            wf = flagged_keys(walk[name])
            comparison[name]["walk_forward"] = {"holdout_flags": len(wf), "jaccard_with_frozen": jaccard(wf, flags),
                                                "monthly_refit_jaccard_mean": _mean(s["jaccard"] for s in walk_stability[name]),
                                                "monthly_refit_jaccard_min": _min(s["jaccard"] for s in walk_stability[name])}

    return {
        "version": EVAL_VERSION,
        "data": {"source": "defillama", "adapters": ["defillama_tvl_history@1", "defillama_token_history@1"],
                 "feature_version": fd.FEATURE_VERSION, "rows": len(rows), "protocols": sorted({r.slug for r in rows}),
                 "note": "Real stored observations only; no synthetic data. Read-only."},
        "splits": {"train_end": TRAIN_END.isoformat(), "calibration": [CALIB_START.isoformat(), CALIB_END.isoformat()],
                   "holdout_start": HOLDOUT_START.isoformat(), "walk_forward_calibration_days": WALK_CALIB_DAYS},
        "target_rate": {"value": round(target_rate, 6), "definition": "tvl-robust-v1 flag rate on calibration-split scored days",
                        "calibration_scored_days": len(calib_scored)},
        "market_context_rule": f"|tvl-robust market_z| >= {MARKET_CONTEXT_Z} or |SOL daily log return robust z| >= {MARKET_CONTEXT_Z} (label only)",
        "attribution_rule": "quantity if |flow_log| >= transparent flow gate, else price_or_other; no_flow_data when no flow",
        "configs": configs,
        "coverage": coverage(rows),
        "metrics": {name: summarize(rows, results, flow_gate) for name, results in models.items()},
        "comparison_holdout": comparison,
        "seed_stability": seed_stability,
        "walk_forward_stability": walk_stability,
        "holdout_flags": {name: flag_list(rows_by_key, results, flow_gate) for name, results in models.items()},
        "case_studies": case_studies(rows_by_key, models, walk, flow_gate),
        "limitations": [
            "No incident labels: precision, recall and exploit prediction cannot be claimed; case studies are qualitative.",
            "DefiLlama is a single source and can revise history.",
            "Kamino per-asset quantities are supplied minus borrowed and sometimes negative at source; such assets drop out of the flow.",
            "Raydium and Orca have no token data: TVL-only.",
            "Drift Trade is a residual balance from 2026-04-02; TVL comparison is disabled from that day.",
            "Kamino starts 2023-10-12, so its training history is short; see coverage and per-group training rows.",
        ],
    }


def _mean(values):
    v = [x for x in values if x is not None]
    return round(float(np.mean(v)), 4) if v else None


def _min(values):
    v = [x for x in values if x is not None]
    return round(float(np.min(v)), 4) if v else None


# --- markdown --------------------------------------------------------------------


def markdown(report: dict) -> str:
    lines = [f"# Multivariate anomaly evaluation ({report['version']})", "",
             f"Generated {report.get('generated_at', '')}. Data: {report['data']['rows']} protocol-days, "
             f"{', '.join(report['data']['protocols'])}. Read-only; no incident labels.", "",
             f"Splits: train <= {report['splits']['train_end']}; calibration {report['splits']['calibration'][0]}..{report['splits']['calibration'][1]}; "
             f"holdout >= {report['splits']['holdout_start']}. Common alert budget (baseline calibration rate): "
             f"{report['target_rate']['value']:.4%}.", "", "## Holdout comparison", "",
             "| Model | Scored | Flags | Rate | Overlap A | Jaccard A | Unique | WF flags | WF Jaccard | Refit Jaccard (mean/min) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in report["comparison_holdout"].items():
        wf = c.get("walk_forward", {})
        lines.append(f"| {name} | {c['holdout_scored']} | {c['holdout_flags']} | {c['holdout_flag_rate']} | {c['overlap_with_baseline']} | "
                     f"{_fmt(c['jaccard_with_baseline'])} | {len(c['unique_vs_baseline'])} | {wf.get('holdout_flags', '—')} | "
                     f"{_fmt(wf.get('jaccard_with_frozen'))} | {_fmt(wf.get('monthly_refit_jaccard_mean'))}/{_fmt(wf.get('monthly_refit_jaccard_min'))} |")
    lines += ["", "## Seed stability (Isolation Forest, holdout flags)", "", "| Feature set | Flags per seed | Pairwise Jaccard mean/min | In all seeds |", "|---|---|---|---|"]
    for name, s in report["seed_stability"].items():
        lines.append(f"| {name} | {s['holdout_flags_per_seed']} | {_fmt(s['pairwise_jaccard_mean'])}/{_fmt(s['pairwise_jaccard_min'])} | {s['flags_in_all_seeds']} |")
    b = report["configs"]["B_transparent_maxz"]
    lines += ["", "## Calibration", "", f"- Transparent gates (q{GATE_QUANTILE:.2f} of training |size|): {b['gates']} (training rows {b['gate_training_rows']}); "
              f"threshold |z| >= {b['threshold']} (calibration rate {_fmt(b['calibration_flag_rate'])})."]
    for name in FEATURE_SETS:
        for g, cfg in report["configs"][name]["groups"].items():
            lines.append(f"- {name} / {g}: status {cfg['status']}, train {cfg['train_rows']} {cfg['train_rows_by_protocol']}, "
                         f"calibration {cfg['calibration_rows']}, threshold {_fmt(cfg['threshold'])}, calibration rate {_fmt(cfg['calibration_flag_rate'])}")
    lines += ["", "## Holdout flags by attribution and context", "", "| Model | Protocol-specific | Market context | Quantity | Price/other | No flow data |", "|---|---|---|---|---|---|"]
    for name, m in report["metrics"].items():
        h = m["by_split"].get("holdout", {})
        lines.append(f"| {name} | {h.get('protocol_specific_flags', 0)} | {h.get('market_context_flags', 0)} | {h.get('attribution_quantity', 0)} | "
                     f"{h.get('attribution_price_or_other', 0)} | {h.get('attribution_no_flow_data', 0)} |")
    lines += ["", "## Case studies (qualitative; not labels)", "", "| Event | Expected | " + " | ".join(report["comparison_holdout"]) + " |",
              "|---|---|" + "---|" * len(report["comparison_holdout"])]
    for c in report["case_studies"]:
        cells = []
        for name in report["comparison_holdout"]:
            m = c["models"][name]
            state = "FLAG" if m["flagged"] else ("no" if m["scored"] else "unscored")
            cells.append(f"{state} ({m['consistency']})")
        lines.append(f"| {c['protocol']} {c['day']} | {c['expected']} | " + " | ".join(cells) + " |")
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in report["limitations"]]
    return "\n".join(lines) + "\n"


def _fmt(v):
    return "—" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))


# --- loading (read-only) -----------------------------------------------------------

PROTOCOLS_QUERY = 'SELECT slug, category::text AS category FROM protocols WHERE "isActive" = true ORDER BY slug'
OBSERVATIONS_QUERY = """
    SELECT o.id, p.slug, o."metricKey" AS metric, o."sourceObservedAt"::date AS day, o."valueText" AS symbol,
           o."valueNumeric" AS value
    FROM metric_observations o JOIN protocols p ON p.id = o."protocolId"
    WHERE p."isActive" = true AND o.status = 'OK'
      AND o."adapterKey" IN ('defillama_tvl_history', 'defillama_token_history')
    ORDER BY p.slug, o."sourceObservedAt", o.id
"""


async def load(dsn: str) -> tuple[list[dict], list[dict]]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            protocols = [dict(r) for r in await conn.fetch(PROTOCOLS_QUERY)]
            observations = [dict(r) for r in await conn.fetch(OBSERVATIONS_QUERY)]
    finally:
        await conn.close()
    return protocols, observations


def prepare(protocol_rows: list[dict], observation_rows: list[dict]) -> list[Row]:
    inputs = fd.inputs_from_rows(protocol_rows, observation_rows)
    series = defaultdict(list)
    for r in observation_rows:
        if r["metric"] == "tvl.usd.daily" and r["value"] is not None:
            series[r["slug"]].append(tvl_robust.DailyObservation(r["day"], float(r["value"]), r["id"]))
    baseline = tvl_robust.compute_scores(dict(series))
    features = fd.compute_daily_features(inputs)
    return build_rows(features, baseline, inputs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline multivariate anomaly evaluation")
    parser.add_argument("--output-dir", required=True, help="directory for the JSON and Markdown reports (keep it outside the repo)")
    args = parser.parse_args()
    from app.config import settings

    protocols, observations = asyncio.run(load(settings.DATABASE_URL))
    report = evaluate(prepare(protocols, observations))
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "multivariate_evaluation.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    (out / "multivariate_evaluation.md").write_text(markdown(report))
    for name, c in report["comparison_holdout"].items():
        print(f"{name:28} holdout flags {c['holdout_flags']:4} rate {c['holdout_flag_rate']} jaccard_A {_fmt(c['jaccard_with_baseline'])}")
    print(f"reports written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
