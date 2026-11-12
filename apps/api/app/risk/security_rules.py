"""Security-dimension rules: pure functions from stored observations to evidence.

Rule set ``security-rules/1``:

REG-01 Registry owner
    The account's owner matches the owner recorded when the address was
    verified -> LOW; a different owner -> HIGH (the registered address may be
    wrong, or the account was reassigned).

SEC-01 Upgradeability
    Immutable -> INFO. Immutability removes upgrade-key risk, but it also
    means a vulnerability in the deployed code cannot be patched, so it is
    reported as a property, never as LOW risk.
    Upgradeable -> MEDIUM, naming the authority. Whether that key is a
    multisig, governance or timelock account is not verified yet (Phase 2).

SEC-02 Upgrade-authority change
    A change between two successful readings within the last 7 days -> HIGH.
    At least two successful readings and no change in that window -> LOW.
    Fewer than two readings -> UNKNOWN (change detection needs a baseline).

SEC-03 Recent redeployment
    Last deploy within REDEPLOY_REVIEW_WINDOW (72 h) -> MEDIUM; otherwise LOW.

    Why 72 hours: a redeploy is an event that warrants investigation (release
    notes, audit status, governance approval), not proof of compromise. Most
    malicious or faulty upgrades are exploited or noticed within hours to a
    few days, and 72 hours keeps a Friday deploy visible through a weekend to
    the next working-day review, while not flagging routine releases forever.
    Elapsed time is estimated from slots (context slot - deploy slot) at the
    nominal 400 ms per slot; real slots are slightly slower, so the estimate
    errs towards keeping a deploy inside the window a little longer.

Freshness (every rule)
    An observation is usable only if its status is OK and it was collected
    within STALE_AFTER (3x the 15-minute collection interval). Otherwise the
    rule yields UNKNOWN with the reason, never LOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.adapters.base import AddressKind, RegisteredAddress
from app.adapters.onchain_security import (
    METRIC_AUTHORITY,
    METRIC_DEPLOY_SLOT,
    METRIC_OWNER,
    METRIC_UPGRADEABLE,
)
from app.risk.evidence import (
    DimensionAssessment,
    Evidence,
    EvidenceLevel,
    ObservationRecord,
    aggregate,
)

RULE_VERSION = "security-rules/1"
COLLECTION_INTERVAL = timedelta(minutes=15)
STALE_AFTER = 3 * COLLECTION_INTERVAL
CHANGE_ALERT_WINDOW = timedelta(days=7)
HISTORY_WINDOW = timedelta(days=30)
REDEPLOY_REVIEW_WINDOW = timedelta(hours=72)
NOMINAL_SLOT_SECONDS = Decimal("0.4")

SECURITY_METRICS = (METRIC_OWNER, METRIC_UPGRADEABLE, METRIC_AUTHORITY, METRIC_DEPLOY_SLOT)
CHANGE_TRACKED_METRICS = (METRIC_UPGRADEABLE, METRIC_AUTHORITY)


@dataclass(frozen=True)
class ObservedChange:
    previous: ObservationRecord
    current: ObservationRecord


@dataclass
class AddressHistory:
    """Everything the rules need about one registered address.

    ``latest``: newest observation per metric, any status.
    ``ok_count`` / ``first_ok``: successful readings within HISTORY_WINDOW.
    ``last_change``: most recent value change between consecutive successful
    readings within HISTORY_WINDOW.
    """

    address: RegisteredAddress
    latest: dict[str, ObservationRecord] = field(default_factory=dict)
    ok_count: dict[str, int] = field(default_factory=dict)
    first_ok: dict[str, ObservationRecord] = field(default_factory=dict)
    last_change: dict[str, ObservedChange] = field(default_factory=dict)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _fmt_time(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _short(key: str | None) -> str:
    return "none" if key is None else f"{key[:6]}…{key[-4:]}" if len(key) > 12 else key


def unusable_reason(observation: ObservationRecord | None, now: datetime) -> str | None:
    """None when the observation can support a conclusion; otherwise why not."""
    if observation is None:
        return "no observation has been collected yet"
    if observation.status == "ERROR":
        return f"the last measurement failed ({observation.error_code})"
    if observation.status == "MISSING":
        return f"the source reported no data ({observation.error_code or 'missing'})"
    if observation.status == "STALE":
        return "the source reported stale data"
    age = _as_utc(now) - _as_utc(observation.collected_at)
    if age > STALE_AFTER:
        return f"the last successful reading is stale (collected {_fmt_time(observation.collected_at)})"
    return None


class _RuleBuilder:
    def __init__(self, history: AddressHistory):
        self.history = history
        address = history.address
        self.prefix = f"Via shared dependency {address.label}: " if address.is_shared_dependency else ""

    def make(self, rule_id, indicator, level, explanation, observations) -> Evidence:
        return Evidence(
            rule_id=rule_id,
            indicator=indicator,
            level=level,
            explanation=self.prefix + explanation,
            address=self.history.address,
            observations=tuple(o for o in observations if o is not None),
            rule_version=RULE_VERSION,
        )

    def unknown(self, rule_id, indicator, reason, observations) -> Evidence:
        return self.make(rule_id, indicator, EvidenceLevel.UNKNOWN, f"Could not verify: {reason}.", observations)


def rule_registry_owner(history: AddressHistory, now: datetime) -> Evidence:
    rb = _RuleBuilder(history)
    observation = history.latest.get(METRIC_OWNER)
    reason = unusable_reason(observation, now)
    if reason:
        return rb.unknown("REG-01", "registry_owner", reason, [observation])
    expected = history.address.expected_owner
    if expected is None:
        return rb.unknown("REG-01", "registry_owner", "no expected owner is recorded for this address", [observation])
    if observation.value_text != expected:
        return rb.make(
            "REG-01",
            "registry_owner",
            EvidenceLevel.HIGH,
            f"Account is owned by {observation.value_text} but the registry expects {expected}; "
            "the registered address may be wrong or the account was reassigned.",
            [observation],
        )
    return rb.make(
        "REG-01", "registry_owner", EvidenceLevel.LOW, f"Account is owned by the expected program {expected}.", [observation]
    )


def rule_upgradeability(history: AddressHistory, now: datetime) -> Evidence:
    rb = _RuleBuilder(history)
    upgradeable = history.latest.get(METRIC_UPGRADEABLE)
    reason = unusable_reason(upgradeable, now)
    if reason:
        return rb.unknown("SEC-01", "upgradeability", reason, [upgradeable])
    if upgradeable.value_numeric == 0:
        return rb.make(
            "SEC-01",
            "upgradeability",
            EvidenceLevel.INFO,
            "Program is immutable: nobody can change its code. This removes upgrade-key risk, "
            "but any vulnerability in the deployed code cannot be patched.",
            [upgradeable],
        )
    authority = history.latest.get(METRIC_AUTHORITY)
    reason = unusable_reason(authority, now)
    if reason:
        return rb.unknown("SEC-01", "upgradeability", f"program is upgradeable but its authority is unknown: {reason}", [upgradeable, authority])
    return rb.make(
        "SEC-01",
        "upgradeability",
        EvidenceLevel.MEDIUM,
        f"Program is upgradeable by authority {authority.value_text}. Whether that key is a multisig, "
        "governance or timelock-controlled account has not been verified yet.",
        [upgradeable, authority],
    )


def rule_authority_change(history: AddressHistory, now: datetime) -> Evidence:
    rb = _RuleBuilder(history)
    upgradeable = history.latest.get(METRIC_UPGRADEABLE)
    reason = unusable_reason(upgradeable, now)
    if reason:
        return rb.unknown("SEC-02", "authority_change", reason, [upgradeable])

    metrics = [METRIC_UPGRADEABLE]
    if upgradeable.value_numeric == 1:
        authority = history.latest.get(METRIC_AUTHORITY)
        reason = unusable_reason(authority, now)
        if reason:
            return rb.unknown("SEC-02", "authority_change", reason, [upgradeable, authority])
        metrics.append(METRIC_AUTHORITY)

    changes = [history.last_change[m] for m in CHANGE_TRACKED_METRICS if m in history.last_change]
    recent = [c for c in changes if _as_utc(now) - _as_utc(c.current.collected_at) <= CHANGE_ALERT_WINDOW]
    if recent:
        change = max(recent, key=lambda c: c.current.collected_at)
        between = (
            f"between {_fmt_time(change.previous.collected_at)} and {_fmt_time(change.current.collected_at)}"
            f" (first seen at slot {change.current.source_slot})"
        )
        if change.current.metric_key == METRIC_AUTHORITY:
            text = (
                f"Upgrade authority changed from {change.previous.value_text} to "
                f"{change.current.value_text} {between}."
            )
        elif change.current.value_numeric == 0:
            text = f"Program became immutable (upgrade authority removed) {between}."
        else:
            text = f"Program became upgradeable {between}."
        return rb.make(
            "SEC-02",
            "authority_change",
            EvidenceLevel.HIGH,
            text + " An authority change can precede a malicious upgrade and should be verified with the team.",
            [change.previous, change.current],
        )

    readings = min(history.ok_count.get(m, 0) for m in metrics)
    if readings < 2:
        return rb.unknown(
            "SEC-02",
            "authority_change",
            f"change detection needs at least two successful readings ({readings} so far)",
            [history.latest.get(m) for m in metrics],
        )
    since = max(_as_utc(history.first_ok[m].collected_at) for m in metrics if m in history.first_ok)
    text = f"No upgrade-authority change observed across {readings} readings since {_fmt_time(since)}."
    if changes:
        older = max(changes, key=lambda c: c.current.collected_at)
        text += f" An earlier change was observed at {_fmt_time(older.current.collected_at)}, outside the 7-day alert window."
    return rb.make(
        "SEC-02",
        "authority_change",
        EvidenceLevel.LOW,
        text,
        [history.first_ok.get(m) for m in metrics] + [history.latest.get(m) for m in metrics],
    )


def rule_recent_redeploy(history: AddressHistory, now: datetime) -> Evidence:
    rb = _RuleBuilder(history)
    deploy = history.latest.get(METRIC_DEPLOY_SLOT)
    if deploy is not None and deploy.status == "MISSING" and deploy.error_code == "NOT_APPLICABLE_LEGACY_LOADER":
        return rb.make(
            "SEC-03",
            "recent_redeploy",
            EvidenceLevel.INFO,
            "Not applicable: the program uses a legacy loader that cannot be redeployed.",
            [deploy],
        )
    reason = unusable_reason(deploy, now)
    if reason:
        return rb.unknown("SEC-03", "recent_redeploy", reason, [deploy])
    if deploy.source_slot is None:
        return rb.unknown("SEC-03", "recent_redeploy", "the reading has no reference slot", [deploy])
    elapsed_slots = deploy.source_slot - int(deploy.value_numeric)
    if elapsed_slots < 0:
        return rb.unknown("SEC-03", "recent_redeploy", "the deploy slot is after the reference slot", [deploy])

    elapsed = timedelta(seconds=float(elapsed_slots * NOMINAL_SLOT_SECONDS))
    hours = elapsed.total_seconds() / 3600
    deploy_slot = int(deploy.value_numeric)
    if elapsed <= REDEPLOY_REVIEW_WINDOW:
        return rb.make(
            "SEC-03",
            "recent_redeploy",
            EvidenceLevel.MEDIUM,
            f"Program was redeployed about {hours:.0f} hours ago (slot {deploy_slot}). A redeployment "
            "warrants investigation (release notes, audit, governance approval); it is not evidence "
            "of compromise.",
            [deploy],
        )
    return rb.make(
        "SEC-03",
        "recent_redeploy",
        EvidenceLevel.LOW,
        f"Last deployed about {hours / 24:.0f} days ago (slot {deploy_slot}); no redeployment within "
        "the 72-hour review window.",
        [deploy],
    )


def evaluate_address(history: AddressHistory, now: datetime) -> list[Evidence]:
    evidence = [rule_registry_owner(history, now)]
    if history.address.kind is AddressKind.PROGRAM:
        evidence += [
            rule_upgradeability(history, now),
            rule_authority_change(history, now),
            rule_recent_redeploy(history, now),
        ]
    return evidence


def assess_security(histories: list[AddressHistory], now: datetime) -> DimensionAssessment:
    evidence: list[Evidence] = []
    for history in histories:
        evidence.extend(evaluate_address(history, now))
    return aggregate("security", evidence, RULE_VERSION)
