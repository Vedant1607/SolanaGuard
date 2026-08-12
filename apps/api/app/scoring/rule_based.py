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


def score_from_snapshots(latest_tvl: float, previous_tvl: float, latest_tx: int, previous_tx: int) -> RuleBasedScore:
    reasons = []
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
    return RuleBasedScore(overall_score=round(score, 1), risk_level=level, explanation=explanation)