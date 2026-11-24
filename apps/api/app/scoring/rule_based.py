"""
Phase 1 rule-based risk scorer — simple thresholds, no ML.
Compares a protocol's latest snapshot to its prior one.
RiskScore.method distinguishes these "rule_based" rows from the
"ml" rows the teammate's branch will add later — both coexist.
"""

from dataclasses import dataclass


@dataclass
class RuleBasedScore:
    overall_score: float
    risk_level: str
    explanation: str


def score_from_snapshots(
    latest_tvl: float,
    previous_tvl: float,
    latest_tx: int | None,
    previous_tx: int | None,
) -> RuleBasedScore:
    """Score from two consecutive snapshots. A ``None`` transaction count means
    the count was unavailable: the transaction rule is skipped (never treated as
    zero) and the score is based on TVL only."""
    reasons = []
    tx_available = latest_tx is not None and previous_tx is not None
    score = 15.0  # baseline for "nothing notable happened"

    tvl_change_pct = ((latest_tvl - previous_tvl) / previous_tvl) * 100 if previous_tvl > 0 else 0.0
    if tvl_change_pct <= -20:
        score += 55
        reasons.append(f"TVL dropped {abs(tvl_change_pct):.1f}% since last snapshot")
    elif tvl_change_pct <= -10:
        score += 35
        reasons.append(f"TVL dropped {abs(tvl_change_pct):.1f}% since last snapshot")
    elif tvl_change_pct <= -5:
        score += 15
        reasons.append(f"TVL down {abs(tvl_change_pct):.1f}% since last snapshot")

    if tx_available:
        tx_ratio = latest_tx / previous_tx if previous_tx > 0 else 1.0
        if tx_ratio >= 4:
            score += 30
            reasons.append(f"Transaction count {tx_ratio:.1f}x above last snapshot")
        elif tx_ratio >= 2.5:
            score += 15
            reasons.append(f"Transaction count {tx_ratio:.1f}x above last snapshot")

    score = min(100.0, score)
    if score >= 75:   level = "CRITICAL"
    elif score >= 55: level = "HIGH"
    elif score >= 35: level = "MEDIUM"
    else:             level = "LOW"

    explanation = "; ".join(reasons) if reasons else "No significant change since last snapshot"
    if not tx_available:
        explanation += " (transaction count unavailable; scored on TVL only)"
    return RuleBasedScore(overall_score=round(score, 1), risk_level=level, explanation=explanation)