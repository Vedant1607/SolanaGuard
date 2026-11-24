import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.adapters.base import AddressKind, RegisteredAddress
from app.adapters.onchain_security import (
    METRIC_AUTHORITY,
    METRIC_DEPLOY_SLOT,
    METRIC_OWNER,
    METRIC_UPGRADEABLE,
)
from app.risk.evidence import DimensionLevel, EvidenceLevel, ObservationRecord, aggregate, Evidence
from app.risk.security_rules import (
    AddressHistory,
    ObservedChange,
    assess_security,
    evaluate_address,
    rule_authority_change,
    rule_recent_redeploy,
    rule_registry_owner,
    rule_upgradeability,
)

NOW = datetime(2026, 11, 12, 12, 0)  # naive UTC, as stored
UPG = "BPFLoaderUpgradeab1e11111111111111111111111"
AUTH_A = "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK"
AUTH_B = "8jj7zJgdr5bDndc7evM74FMGwzLPmd4u4QxNzFi1BMai"
CONTEXT_SLOT = 468_000_000
SLOTS_PER_HOUR = 9_000  # 3600 s / 0.4 s
_ids = count()


def obs(metric, *, status="OK", numeric=None, text=None, age=timedelta(minutes=5), slot=CONTEXT_SLOT, error=None):
    unit = {"registry.owner": "pubkey", METRIC_AUTHORITY: "pubkey", METRIC_UPGRADEABLE: "bool"}.get(metric, "slot")
    return ObservationRecord(
        id=f"obs-{next(_ids)}",
        metric_key=metric,
        status=status,
        value_numeric=None if numeric is None else Decimal(numeric),
        value_text=text,
        unit=unit,
        source="solana-rpc",
        source_slot=slot,
        source_observed_at=None,
        collected_at=NOW - age,
        adapter_key="onchain_security",
        adapter_version="1",
        error_code=error,
    )


def program(shared=False):
    return RegisteredAddress(
        id="addr-1", address="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", kind=AddressKind.PROGRAM,
        label="Raydium AMM v4", is_shared_dependency=shared, expected_owner=UPG,
    )


def healthy_history(readings=10, deploy_hours_ago=24 * 100, shared=False):
    latest = {
        METRIC_OWNER: obs(METRIC_OWNER, text=UPG),
        METRIC_UPGRADEABLE: obs(METRIC_UPGRADEABLE, numeric=1),
        METRIC_AUTHORITY: obs(METRIC_AUTHORITY, text=AUTH_A),
        METRIC_DEPLOY_SLOT: obs(METRIC_DEPLOY_SLOT, numeric=CONTEXT_SLOT - deploy_hours_ago * SLOTS_PER_HOUR),
    }
    first = {m: obs(m, numeric=o.value_numeric, text=o.value_text, age=timedelta(days=3)) for m, o in latest.items()}
    return AddressHistory(
        address=program(shared),
        latest=latest,
        ok_count={m: readings for m in latest},
        first_ok=first,
    )


class RegistryOwnerRuleTests(unittest.TestCase):
    def test_expected_owner_is_low_and_mismatch_is_high(self):
        history = healthy_history()
        self.assertIs(rule_registry_owner(history, NOW).level, EvidenceLevel.LOW)
        history.latest[METRIC_OWNER] = obs(METRIC_OWNER, text="11111111111111111111111111111111")
        evidence = rule_registry_owner(history, NOW)
        self.assertIs(evidence.level, EvidenceLevel.HIGH)
        self.assertIn("registry expects", evidence.explanation)


class UpgradeabilityRuleTests(unittest.TestCase):
    def test_upgradeable_is_medium_and_names_the_authority(self):
        evidence = rule_upgradeability(healthy_history(), NOW)
        self.assertIs(evidence.level, EvidenceLevel.MEDIUM)
        self.assertIn(AUTH_A, evidence.explanation)
        self.assertIn("not been verified", evidence.explanation)

    def test_immutable_is_informational_never_low(self):
        history = healthy_history()
        history.latest[METRIC_UPGRADEABLE] = obs(METRIC_UPGRADEABLE, numeric=0)
        del history.latest[METRIC_AUTHORITY]
        evidence = rule_upgradeability(history, NOW)
        self.assertIs(evidence.level, EvidenceLevel.INFO)
        self.assertIn("cannot be patched", evidence.explanation)

    def test_upgradeable_with_failed_authority_read_is_unknown(self):
        history = healthy_history()
        history.latest[METRIC_AUTHORITY] = obs(METRIC_AUTHORITY, status="ERROR", error="TIMEOUT")
        self.assertIs(rule_upgradeability(history, NOW).level, EvidenceLevel.UNKNOWN)


class AuthorityChangeRuleTests(unittest.TestCase):
    def test_first_reading_is_unknown(self):
        evidence = rule_authority_change(healthy_history(readings=1), NOW)
        self.assertIs(evidence.level, EvidenceLevel.UNKNOWN)
        self.assertIn("at least two successful readings", evidence.explanation)

    def test_stable_readings_are_low(self):
        evidence = rule_authority_change(healthy_history(readings=12), NOW)
        self.assertIs(evidence.level, EvidenceLevel.LOW)
        self.assertIn("12 readings", evidence.explanation)

    def test_recent_change_is_high_and_cites_both_observations(self):
        history = healthy_history()
        previous = obs(METRIC_AUTHORITY, text=AUTH_A, age=timedelta(days=2, minutes=15))
        current = obs(METRIC_AUTHORITY, text=AUTH_B, age=timedelta(days=2), slot=467_000_000)
        history.latest[METRIC_AUTHORITY] = obs(METRIC_AUTHORITY, text=AUTH_B)
        history.last_change[METRIC_AUTHORITY] = ObservedChange(previous, current)
        evidence = rule_authority_change(history, NOW)
        self.assertIs(evidence.level, EvidenceLevel.HIGH)
        self.assertIn(f"from {AUTH_A} to {AUTH_B}", evidence.explanation)
        self.assertIn("slot 467000000", evidence.explanation)
        self.assertEqual([o.id for o in evidence.observations], [previous.id, current.id])

    def test_becoming_immutable_is_reported_as_a_change(self):
        history = healthy_history()
        history.latest[METRIC_UPGRADEABLE] = obs(METRIC_UPGRADEABLE, numeric=0)
        del history.latest[METRIC_AUTHORITY]
        history.last_change[METRIC_UPGRADEABLE] = ObservedChange(
            obs(METRIC_UPGRADEABLE, numeric=1, age=timedelta(hours=2)),
            obs(METRIC_UPGRADEABLE, numeric=0, age=timedelta(hours=1)),
        )
        evidence = rule_authority_change(history, NOW)
        self.assertIs(evidence.level, EvidenceLevel.HIGH)
        self.assertIn("became immutable", evidence.explanation)

    def test_change_older_than_alert_window_is_mentioned_but_low(self):
        history = healthy_history()
        history.last_change[METRIC_AUTHORITY] = ObservedChange(
            obs(METRIC_AUTHORITY, text=AUTH_B, age=timedelta(days=10, minutes=15)),
            obs(METRIC_AUTHORITY, text=AUTH_A, age=timedelta(days=10)),
        )
        evidence = rule_authority_change(history, NOW)
        self.assertIs(evidence.level, EvidenceLevel.LOW)
        self.assertIn("outside the 7-day alert window", evidence.explanation)


class RedeployRuleTests(unittest.TestCase):
    def test_deploy_within_72_hours_is_medium_and_not_called_compromise(self):
        evidence = rule_recent_redeploy(healthy_history(deploy_hours_ago=10), NOW)
        self.assertIs(evidence.level, EvidenceLevel.MEDIUM)
        self.assertIn("about 10 hours ago", evidence.explanation)
        self.assertIn("not evidence of compromise", evidence.explanation)

    def test_boundary_and_old_deploys(self):
        self.assertIs(rule_recent_redeploy(healthy_history(deploy_hours_ago=72), NOW).level, EvidenceLevel.MEDIUM)
        old = rule_recent_redeploy(healthy_history(deploy_hours_ago=73), NOW)
        self.assertIs(old.level, EvidenceLevel.LOW)
        self.assertIn("72-hour review window", old.explanation)

    def test_inconsistent_or_unanchored_slots_are_unknown(self):
        history = healthy_history()
        history.latest[METRIC_DEPLOY_SLOT] = obs(METRIC_DEPLOY_SLOT, numeric=CONTEXT_SLOT + 5)
        self.assertIs(rule_recent_redeploy(history, NOW).level, EvidenceLevel.UNKNOWN)
        history.latest[METRIC_DEPLOY_SLOT] = obs(METRIC_DEPLOY_SLOT, numeric=1, slot=None)
        self.assertIs(rule_recent_redeploy(history, NOW).level, EvidenceLevel.UNKNOWN)

    def test_legacy_loader_is_informational(self):
        history = healthy_history()
        history.latest[METRIC_DEPLOY_SLOT] = obs(
            METRIC_DEPLOY_SLOT, status="MISSING", error="NOT_APPLICABLE_LEGACY_LOADER"
        )
        self.assertIs(rule_recent_redeploy(history, NOW).level, EvidenceLevel.INFO)


# Rules that read each metric (for an upgradeable program).
DEPENDS_ON = {
    METRIC_OWNER: ("REG-01",),
    METRIC_UPGRADEABLE: ("SEC-01", "SEC-02"),
    METRIC_AUTHORITY: ("SEC-01", "SEC-02"),
    METRIC_DEPLOY_SLOT: ("SEC-03",),
}


class MissingDataNeverLowTests(unittest.TestCase):
    """Adjustment E: missing, stale or failed observations never produce LOW."""

    def degraded_variants(self):
        yield "error", lambda m: obs(m, status="ERROR", error="HTTP_429")
        yield "missing", lambda m: obs(m, status="MISSING", error="ACCOUNT_NOT_FOUND")
        yield "source-stale", lambda m: obs(m, status="STALE", text="x", numeric=None)
        yield "collection-stale", lambda m: obs(
            m, text=UPG if m == METRIC_OWNER else (AUTH_A if m == METRIC_AUTHORITY else None),
            numeric=None if m in (METRIC_OWNER, METRIC_AUTHORITY) else 1, age=timedelta(minutes=46),
        )

    def test_every_rule_returns_unknown_for_every_degraded_input(self):
        for name, make in self.degraded_variants():
            for metric in (METRIC_OWNER, METRIC_UPGRADEABLE, METRIC_AUTHORITY, METRIC_DEPLOY_SLOT):
                history = healthy_history()
                history.latest[metric] = make(metric)
                evidence = {e.rule_id: e for e in evaluate_address(history, NOW)}
                for rule_id in DEPENDS_ON[metric]:
                    item = evidence[rule_id]
                    with self.subTest(variant=name, metric=metric, rule=rule_id):
                        self.assertIs(item.level, EvidenceLevel.UNKNOWN)
                        self.assertIn("Could not verify", item.explanation)
                dimension = assess_security([history], NOW)
                with self.subTest(variant=name, metric=metric, rule="dimension"):
                    self.assertIs(dimension.level, DimensionLevel.UNKNOWN)

    def test_no_observations_at_all_is_unknown(self):
        history = AddressHistory(address=program())
        dimension = assess_security([history], NOW)
        self.assertIs(dimension.level, DimensionLevel.UNKNOWN)
        self.assertEqual(dimension.assessed, 0)
        self.assertEqual(dimension.total, 4)

    def test_no_addresses_is_unknown(self):
        self.assertIs(assess_security([], NOW).level, DimensionLevel.UNKNOWN)


class AggregationTests(unittest.TestCase):
    def evidence(self, level):
        return Evidence("X", "x", level, "x", None, (), "v")

    def test_high_is_never_hidden_by_unknown(self):
        result = aggregate("security", [self.evidence(EvidenceLevel.UNKNOWN), self.evidence(EvidenceLevel.HIGH)], "v")
        self.assertIs(result.level, DimensionLevel.HIGH)
        self.assertIn("could not be verified", result.explanation)

    def test_unknown_beats_low_and_medium(self):
        result = aggregate("security", [self.evidence(EvidenceLevel.MEDIUM), self.evidence(EvidenceLevel.UNKNOWN)], "v")
        self.assertIs(result.level, DimensionLevel.UNKNOWN)

    def test_info_only_is_unknown_and_info_never_lowers(self):
        self.assertIs(aggregate("s", [self.evidence(EvidenceLevel.INFO)], "v").level, DimensionLevel.UNKNOWN)
        mixed = aggregate("s", [self.evidence(EvidenceLevel.INFO), self.evidence(EvidenceLevel.MEDIUM)], "v")
        self.assertIs(mixed.level, DimensionLevel.MEDIUM)

    def test_healthy_upgradeable_program_is_medium_with_full_coverage(self):
        result = assess_security([healthy_history()], NOW)
        self.assertIs(result.level, DimensionLevel.MEDIUM)
        self.assertEqual((result.assessed, result.total), (4, 4))
        self.assertIn("SEC-01 on Raydium AMM v4", result.explanation)

    def test_shared_dependency_is_labelled(self):
        result = assess_security([healthy_history(shared=True)], NOW)
        self.assertTrue(all(e.explanation.startswith("Via shared dependency") for e in result.evidence))

    def test_every_evidence_item_cites_observations_with_provenance(self):
        for item in assess_security([healthy_history()], NOW).evidence:
            with self.subTest(rule=item.rule_id):
                self.assertTrue(item.observations)
                for o in item.observations:
                    self.assertTrue(o.id and o.source and o.collected_at)
                self.assertEqual(item.rule_version, "security-rules/1")


if __name__ == "__main__":
    unittest.main()
