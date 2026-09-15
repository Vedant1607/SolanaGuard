"""
Risk Scorer — Component 2a.

Combines 6 weighted sub-scores into one composite 0-100 risk score, per
the RiskScoreResult contract. Two of the six (smart_contract_score,
governance_score) have NO real data source anywhere in our current
pipeline -- there's no audit feed, no DAO/governance feed. Those are
static placeholders until a real data source is wired in; this is called
out explicitly (and factored into `confidence`) rather than hidden.

sentiment_score is stubbed at 0 per the handoff doc's build order --
Component 3 (Sentiment Analyzer) isn't built yet.
"""

from datetime import datetime, timezone

# --- Weights ----------------------------------------------------------
# These determine how much each sub-score contributes to the overall
# risk score. Anomaly and liquidity get the most weight because they're
# the most directly observable, real-time risk signals we have. Smart
# contract and governance get lower weight *because* they're placeholders
# right now -- we don't want unreliable inputs dominating the score.
WEIGHTS = {
    "liquidity_score": 0.25,
    "anomaly_score": 0.25,
    "market_score": 0.20,
    "smart_contract_score": 0.15,
    "governance_score": 0.10,
    "sentiment_score": 0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

# Placeholder smart-contract / governance risk scores per protocol.
# Lower = safer. These are rough, manually-assigned stand-ins based on
# public reputation (e.g. audit history, time in market) -- NOT derived
# from any real data feed yet. Replace once a real source exists.
SMART_CONTRACT_PLACEHOLDER = {
    "raydium": 25, "orca": 20, "kamino": 35,
    "marginfi": 40, "marinade": 15, "jito": 20,
}
GOVERNANCE_PLACEHOLDER = {
    "raydium": 30, "orca": 25, "kamino": 35,
    "marginfi": 35, "marinade": 20, "jito": 25,
}


def _liquidity_score(row) -> float:
    """Higher = riskier. Driven by liquidity depth relative to TVL (thin
    liquidity relative to TVL means a large trade moves price a lot) and
    how sharply liquidity has been dropping."""
    depth_ratio = row["liquidity_depth"] / max(row["tvl_usd"], 1)
    thinness_risk = max(0, (0.65 - depth_ratio)) * 200  # normal ratio ~0.55-0.75
    drop_risk = max(0, -row["liquidity_pct_change_1h"]) * 300
    zscore_risk = max(0, -row["liquidity_zscore_24h"]) * 15
    return float(min(thinness_risk + drop_risk + zscore_risk, 100))


def _market_score(row) -> float:
    """Higher = riskier. Driven by volume/tx volatility -- erratic trading
    activity is itself a risk signal, independent of whether the anomaly
    detector flagged a specific event."""
    vol_risk = min(abs(row["volume_zscore_24h"]) * 12, 60)
    tx_risk = min(abs(row["tx_zscore_24h"]) * 10, 40)
    return float(min(vol_risk + tx_risk, 100))


def _risk_level(score: float) -> str:
    if score >= 80:
        return "CRITICAL"
    elif score >= 60:
        return "HIGH"
    elif score >= 35:
        return "MEDIUM"
    return "LOW"


def compute_risk_score(row, anomaly_score: float, sentiment_score: float = 0.0) -> dict:
    """
    row: a feature-engineered snapshot (pandas Series) for one protocol/hour,
         as produced by features.build_features()
    anomaly_score: 0-100 output from the anomaly detector for this same row
    sentiment_score: 0-100 risk contribution from sentiment (0 = stubbed/neutral
                      until Component 3 exists; NOT the raw -1..+1 sentiment value)
    """
    slug = row["protocol_slug"]

    components = {
        "liquidity_score": round(_liquidity_score(row), 1),
        "smart_contract_score": float(SMART_CONTRACT_PLACEHOLDER.get(slug, 50)),
        "market_score": round(_market_score(row), 1),
        "governance_score": float(GOVERNANCE_PLACEHOLDER.get(slug, 50)),
        "sentiment_score": round(sentiment_score, 1),
        "anomaly_score": round(anomaly_score, 1),
    }

    overall = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)

    # Confidence is lower right now because 2 of 6 inputs are placeholders,
    # not real data. This is an honest signal to the rest of the system,
    # not just decoration -- once smart_contract/governance/sentiment have
    # real data sources, confidence should rise.
    placeholder_weight = WEIGHTS["smart_contract_score"] + WEIGHTS["governance_score"] + WEIGHTS["sentiment_score"]
    confidence = round(1.0 - placeholder_weight * 0.6, 2)

    return {
        "protocol_slug": slug,
        "overall_score": round(overall, 1),
        "risk_level": _risk_level(overall),
        "components": components,
        "confidence": confidence,
        "forecasted_score_24h": None,   # filled in by forecaster.py
        "forecasted_level_24h": None,   # filled in by forecaster.py
        "method": "ml",
        "scored_at": row["snapshot_at"] if isinstance(row["snapshot_at"], str)
                      else row["snapshot_at"].isoformat(),
    }
