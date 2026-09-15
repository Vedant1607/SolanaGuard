DATA_DIR = Path(__file__).parent / "data" / "synthetic"
MODELS_DIR = Path(__file__).parent / "models"
Anomaly Detector — Component 1.

One Isolation Forest per protocol CATEGORY (not per protocol, not global):
DEX, LENDING, and LIQUID_STAKING each have genuinely different "normal"
volatility, so a single global model would either be too jumpy for DEX
or too blind for LIQUID_STAKING. A per-protocol model would be more
precise eventually, but needs more history than 3 weeks gives us right now
— category-level is the right grain for a first working model.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

# pyright: reportMissingModuleSource=false
try:
    from sklearn.ensemble import IsolationForest
except ImportError as exc:
    raise ImportError(
        "scikit-learn is required to run the anomaly detector. Install it with `pip install scikit-learn`."
    ) from exc

from app.ml.features import build_features, FEATURE_COLUMNS

DATA_DIR = Path(__file__).parent.parent / "data" / "synthetic"
MODELS_DIR = Path(__file__).parent.parent / "models"
MODELS_DIR.mkdir(exist_ok=True)

CONTAMINATION = 0.03  # expected ~3% of hours are anomalous — a starting assumption, tune later
ANOMALY_SCORE_THRESHOLD = 55  # 0-100; below this, is_anomaly=False


def load_protocol_data(slug: str) -> pd.DataFrame:
    with open(DATA_DIR / f"{slug}.json") as f:
        raw = json.load(f)
    return pd.DataFrame(raw)


def train_category_models(all_features: dict[str, pd.DataFrame]) -> dict[str, IsolationForest]:
    """all_features: category -> concatenated feature DataFrame across its protocols."""
    models = {}
    for category, df in all_features.items():
        model = IsolationForest(
            n_estimators=200,
            contamination=CONTAMINATION,
            random_state=42,
        )
        model.fit(df[FEATURE_COLUMNS])
        models[category] = model
    return models


def _score_to_0_100(raw_scores: np.ndarray, all_raw_scores: np.ndarray) -> np.ndarray:
    """
    IsolationForest.decision_function: higher = more normal, lower/negative = more anomalous.
    We flip and min-max scale against the training distribution so scores are stable
    and comparable across protocols within the same category.
    """
    inverted = -raw_scores
    lo, hi = np.percentile(-all_raw_scores, [1, 99])
    scaled = (inverted - lo) / (hi - lo + 1e-9)
    return np.clip(scaled * 100, 0, 100)


def _classify_type(row: pd.Series) -> str:
    """Heuristic: pick the anomaly type matching whichever feature deviated most."""
    candidates = {
        "LIQUIDITY_WITHDRAWAL": -min(row["tvl_zscore_24h"], row["liquidity_zscore_24h"]),
        "TX_SPIKE": row["tx_zscore_24h"] if row["tx_per_wallet_zscore_24h"] < 1.5 else 0,
        "WHALE_MOVEMENT": row["tx_per_wallet_zscore_24h"],
        "PRICE_DEVIATION": row["vol_liq_ratio_zscore_24h"],
    }
    return max(candidates, key=candidates.get)


def _severity(score: float) -> str:
    if score >= 90:
        return "CRITICAL"
    elif score >= 75:
        return "HIGH"
    elif score >= 55:
        return "MEDIUM"
    return "LOW"


def _description(anomaly_type: str, row: pd.Series) -> str:
    descs = {
        "LIQUIDITY_WITHDRAWAL": f"TVL/liquidity dropped sharply ({row['tvl_pct_change_1h']*100:.1f}% in 1h)",
        "TX_SPIKE": f"Transaction volume {row['tx_zscore_24h']:.1f} std devs above 24h baseline",
        "WHALE_MOVEMENT": f"Large trade concentrated among few wallets (tx/wallet z-score {row['tx_per_wallet_zscore_24h']:.1f})",
        "PRICE_DEVIATION": f"Volume/liquidity ratio {row['vol_liq_ratio_zscore_24h']:.1f} std devs above baseline",
    }
    return descs[anomaly_type]


def detect(df_features: pd.DataFrame, model: IsolationForest) -> list[dict]:
    """Returns a list of AnomalyResult dicts, one per row (matching Output 1 contract)."""
    raw_scores = model.decision_function(df_features[FEATURE_COLUMNS])
    scores_0_100 = _score_to_0_100(raw_scores, raw_scores)

    results = []
    for i, (_, row) in enumerate(df_features.iterrows()):
        score = float(scores_0_100[i])
        is_anomaly = score >= ANOMALY_SCORE_THRESHOLD
        anomaly_type = _classify_type(row) if is_anomaly else None
        results.append({
            "protocol_slug": row["protocol_slug"],
            "is_anomaly": bool(is_anomaly),
            "score": round(score, 1),
            "type": anomaly_type,
            "severity": _severity(score) if is_anomaly else None,
            "description": _description(anomaly_type, row) if is_anomaly else None,
            "detected_at": row["snapshot_at"],
        })
    return results


def main():
    protocols = ["raydium", "orca", "kamino", "marginfi", "marinade", "jito"]
    raw_by_protocol = {slug: load_protocol_data(slug) for slug in protocols}

    features_by_protocol = {slug: build_features(df) for slug, df in raw_by_protocol.items()}

    # group features by category for training
    features_by_category = {}
    for slug, feat_df in features_by_protocol.items():
        cat = feat_df["category"].iloc[0]
        features_by_category.setdefault(cat, []).append(feat_df)
    features_by_category = {cat: pd.concat(dfs, ignore_index=True) for cat, dfs in features_by_category.items()}

    print("Training Isolation Forest per category...")
    models = train_category_models(features_by_category)
    for cat, df in features_by_category.items():
        print(f"  {cat}: trained on {len(df)} rows")

    print("\nRunning detection on all protocols...")
    all_results = {}
    for slug, feat_df in features_by_protocol.items():
        cat = feat_df["category"].iloc[0]
        results = detect(feat_df, models[cat])
        all_results[slug] = results
        n_flagged = sum(r["is_anomaly"] for r in results)
        print(f"  {slug:10s}: {n_flagged} hours flagged as anomalous out of {len(results)}")

    out_path = Path(__file__).parent.parent / "data" / "detection_results.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved detection results to {out_path}")

    import joblib
    for cat, model in models.items():
        joblib.dump(model, MODELS_DIR / f"isolation_forest_{cat.lower()}.joblib")
    print(f"Saved trained models to {MODELS_DIR}")


if __name__ == "__main__":
    main()
