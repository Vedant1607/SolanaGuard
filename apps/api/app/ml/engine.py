from pathlib import Path

import joblib
import pandas as pd

from app.ml.features import build_features
from app.ml.anomaly_detector import detect
from app.ml.risk_scorer import compute_risk_score


MODELS_DIR = Path(__file__).parent / "models"

MODEL_FILES = {
    "DEX": "isolation_forest_dex.joblib",
    "LENDING": "isolation_forest_lending.joblib",
    "LIQUID_STAKING": "isolation_forest_liquid_staking.joblib",
}


def load_model(category: str):
    filename = MODEL_FILES.get(category)
    if filename is None:
        raise ValueError(f"No ML model configured for category: {category}")

    model_path = MODELS_DIR / filename

    if not model_path.exists():
        raise FileNotFoundError(f"ML model not found: {model_path}")

    return joblib.load(model_path)


def score_protocol(category: str, snapshots: list[dict]) -> dict | None:
    """
    Run the ML anomaly detector and risk scorer against historical
    snapshots for one protocol.

    Returns None when there is not enough history to build features.
    """

    if len(snapshots) <= 24:
        return None

    df = pd.DataFrame(snapshots)

    features = build_features(df)

    if features.empty:
        return None

    model = load_model(category)

    results = detect(features, model)

    if not results:
        return None

    latest_result = results[-1]
    latest_features = features.iloc[-1]

    ml_result = compute_risk_score(
        latest_features,
        anomaly_score=latest_result["score"],
    )

    ml_result["anomaly"] = latest_result

    return ml_result
