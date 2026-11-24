"""Explainable evidence and per-dimension aggregation.

Observations are raw measurements (``metric_observations``). Evidence is an
interpretation of specific observations by a versioned rule. The two are
never mixed: evidence is computed on read and references the exact
observation rows (id, source, slot and timestamps) it was derived from.

There is no weighted score. A dimension's level is decided by these rules,
in order:

1. Any HIGH or CRITICAL evidence -> the highest such level. Known risk is
   never hidden behind unknowns.
2. Otherwise any UNKNOWN evidence -> UNKNOWN. Missing, stale or failed
   measurements can never produce LOW.
3. Otherwise the highest of LOW / MEDIUM.
4. Only INFO evidence (or none at all) -> UNKNOWN: nothing was assessed.

``INFO`` marks a property worth knowing that is neither good nor bad on its
own (for example, an immutable program). It never lowers a dimension.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.adapters.base import RegisteredAddress


class EvidenceLevel(StrEnum):
    UNKNOWN = "UNKNOWN"
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DimensionLevel(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_SEVERITY = {EvidenceLevel.LOW: 1, EvidenceLevel.MEDIUM: 2, EvidenceLevel.HIGH: 3, EvidenceLevel.CRITICAL: 4}


@dataclass(frozen=True)
class ObservationRecord:
    """A stored observation, as loaded from ``metric_observations``."""

    id: str
    metric_key: str
    status: str
    value_numeric: Decimal | None
    value_text: str | None
    unit: str
    source: str
    source_slot: int | None
    source_observed_at: datetime | None
    collected_at: datetime
    adapter_key: str
    adapter_version: str
    error_code: str | None

    @property
    def display_value(self) -> str | None:
        if self.value_text is not None:
            return self.value_text
        if self.value_numeric is None:
            return None
        if self.unit == "bool":
            return "true" if self.value_numeric == 1 else "false"
        if self.value_numeric == self.value_numeric.to_integral_value():
            return str(int(self.value_numeric))
        return str(self.value_numeric)


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    indicator: str
    level: EvidenceLevel
    explanation: str
    address: RegisteredAddress | None
    observations: tuple[ObservationRecord, ...]
    rule_version: str


@dataclass(frozen=True)
class DimensionAssessment:
    dimension: str
    level: DimensionLevel
    explanation: str
    evidence: tuple[Evidence, ...]
    assessed: int  # evidence items that are not UNKNOWN
    total: int
    rule_version: str


def _describe(evidence: Evidence) -> str:
    where = f" on {evidence.address.label}" if evidence.address else ""
    return f"{evidence.rule_id}{where}"


def aggregate(dimension: str, evidence: list[Evidence], rule_version: str) -> DimensionAssessment:
    total = len(evidence)
    unknown = [e for e in evidence if e.level is EvidenceLevel.UNKNOWN]
    graded = [e for e in evidence if e.level in _SEVERITY]
    assessed = total - len(unknown)

    def build(level: DimensionLevel, explanation: str) -> DimensionAssessment:
        return DimensionAssessment(
            dimension=dimension,
            level=level,
            explanation=explanation,
            evidence=tuple(evidence),
            assessed=assessed,
            total=total,
            rule_version=rule_version,
        )

    if not evidence:
        return build(DimensionLevel.UNKNOWN, "No indicators could be evaluated for this protocol.")

    unknown_note = ""
    if unknown:
        unknown_note = (
            f"{len(unknown)} of {total} indicators could not be verified "
            f"({', '.join(_describe(e) for e in unknown[:3])}{', ...' if len(unknown) > 3 else ''})."
        )

    worst = max((_SEVERITY[e.level] for e in graded), default=0)
    if worst >= _SEVERITY[EvidenceLevel.HIGH]:
        level = next(lvl for lvl, rank in _SEVERITY.items() if rank == worst)
        drivers = [e for e in graded if e.level is level]
        text = f"{len(drivers)} indicator(s) at {level}: " + "; ".join(_describe(e) for e in drivers) + "."
        return build(DimensionLevel(level.value), f"{text} {unknown_note}".strip())

    if unknown:
        return build(DimensionLevel.UNKNOWN, unknown_note)

    if worst == 0:
        return build(DimensionLevel.UNKNOWN, "Only informational properties were observed; nothing was assessed.")

    level = next(lvl for lvl, rank in _SEVERITY.items() if rank == worst)
    drivers = [e for e in graded if e.level is level]
    return build(
        DimensionLevel(level.value),
        f"{len(drivers)} indicator(s) at {level}: " + "; ".join(_describe(e) for e in drivers) + ".",
    )
