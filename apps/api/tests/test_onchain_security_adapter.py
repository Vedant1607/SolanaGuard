import base64
import io
import logging
import os
import struct
import unittest
from datetime import datetime, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import httpx

from app.adapters.base import (
    AdapterContext,
    AddressKind,
    InvalidObservation,
    Observation,
    ObservationStatus,
    RegisteredAddress,
)
from app.adapters.onchain_security import (
    METRIC_AUTHORITY,
    METRIC_DEPLOY_SLOT,
    METRIC_OWNER,
    METRIC_UPGRADEABLE,
    OnchainSecurityAdapter,
)
from app.log_redaction import RedactingFilter
from app.solana.account_reader import AccountReader
from app.solana.loader import BPF_UPGRADEABLE_LOADER, decode_program_account
from tests.solana_fixtures import (
    FAKE_RPC_URL,
    account_value,
    recorded_responses,
    replay_transport,
    with_data,
)

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
UPG = BPF_UPGRADEABLE_LOADER

REGISTRY = [
    ("raydium-amm", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", AddressKind.PROGRAM, UPG),
    ("raydium-clmm", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", AddressKind.PROGRAM, UPG),
    ("raydium-cpmm", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", AddressKind.PROGRAM, UPG),
    ("orca", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", AddressKind.PROGRAM, UPG),
    ("kamino", "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD", AddressKind.PROGRAM, UPG),
    ("kamino-market", "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF", AddressKind.MARKET, "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"),
    ("marinade", "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD", AddressKind.PROGRAM, UPG),
    ("marinade-state", "8szGkuLTAux9XMgZ2vtY39jVSowEcpBfFfD8hXSEqdGC", AddressKind.MARKET, "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD"),
    ("msol", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So", AddressKind.MINT, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
    ("jito-pool", "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", AddressKind.STAKE_POOL, "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy"),
    ("spl-stake-pool", "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy", AddressKind.PROGRAM, UPG),
    ("jitosol", "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", AddressKind.MINT, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
    ("drift", "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH", AddressKind.PROGRAM, UPG),
    ("marginfi", "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA", AddressKind.PROGRAM, UPG),
]


def address(label, addr, kind, owner=None):
    return RegisteredAddress(id=f"addr-{label}", address=addr, kind=kind, label=label, expected_owner=owner)


def context(*addresses):
    return AdapterContext(protocol_id="protocol-1", slug="test", addresses=tuple(addresses), now=NOW)


async def collect(addresses, transport):
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OnchainSecurityAdapter(AccountReader(FAKE_RPC_URL, client))
        return await adapter.collect(context(*addresses))


def by_metric(observations, address_id):
    return {o.metric_key: o for o in observations if o.address_id == address_id}


class RecordedMainnetTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_registry_from_recorded_mainnet_responses(self):
        addresses = [address(*row) for row in REGISTRY]
        observations = await collect(addresses, replay_transport())

        for label, addr, kind, owner in REGISTRY:
            with self.subTest(label=label):
                metrics = by_metric(observations, f"addr-{label}")
                self.assertEqual(metrics[METRIC_OWNER].status, ObservationStatus.OK)
                self.assertEqual(metrics[METRIC_OWNER].value_text, owner)
                if kind is AddressKind.PROGRAM:
                    self.assertEqual(
                        set(metrics), {METRIC_OWNER, METRIC_UPGRADEABLE, METRIC_AUTHORITY, METRIC_DEPLOY_SLOT}
                    )
                    self.assertTrue(all(o.status is ObservationStatus.OK for o in metrics.values()))
                    self.assertEqual(metrics[METRIC_UPGRADEABLE].value_numeric, Decimal(1))
                    self.assertIsInstance(metrics[METRIC_DEPLOY_SLOT].source_slot, int)
                else:
                    self.assertEqual(set(metrics), {METRIC_OWNER})
        self.assertTrue(all(o.source == "solana-rpc" for o in observations))
        self.assertTrue(all(o.source_observed_at is None for o in observations))

    async def test_raydium_values_match_recording(self):
        observations = await collect([address(*REGISTRY[0])], replay_transport())
        metrics = by_metric(observations, "addr-raydium-amm")
        self.assertEqual(metrics[METRIC_AUTHORITY].value_text, "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK")
        self.assertEqual(metrics[METRIC_DEPLOY_SLOT].value_numeric, Decimal(445_763_204))
        self.assertGreater(metrics[METRIC_DEPLOY_SLOT].source_slot, 445_763_204)

    async def test_recorded_invalid_key_rpc_error_becomes_error_observations(self):
        # Real recorded response: RPC rejects the old 33-byte Kamino seed value.
        bad = "KLend2g3cP87fffoy8q1mQqGKjrL9jnmujyAASHa3tsF"
        responses = recorded_responses()
        responses[(bad, 0, 36)] = responses[(bad, 0, 0)]
        observations = await collect([address("bad", bad, AddressKind.PROGRAM)], replay_transport(responses))
        self.assertEqual(len(observations), 4)
        for o in observations:
            self.assertIs(o.status, ObservationStatus.ERROR)
            self.assertEqual(o.error_code, "RPC_-32602")
            self.assertIsNone(o.value_numeric)
            self.assertIsNone(o.value_text)

    async def test_recorded_absent_account_is_missing_not_zero(self):
        # Real recorded response: the old Marginfi seed value has no account.
        absent = "MFv2hWf31Z9kbCa1snEPdcgp168vLLAZnkTHsMgGgAB"
        responses = recorded_responses()
        observations = await collect([address("absent", absent, AddressKind.MARKET)], replay_transport(responses))
        (owner,) = observations
        self.assertIs(owner.status, ObservationStatus.MISSING)
        self.assertEqual(owner.error_code, "ACCOUNT_NOT_FOUND")
        self.assertIsNone(owner.value_text)


class DerivedEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    """Edge cases derived from recorded responses by editing header bytes."""

    def setUp(self):
        self.program = REGISTRY[0]
        self.responses = recorded_responses()
        program_response = self.responses[(self.program[1], 0, 36)]
        self.programdata = decode_program_account(base64.b64decode(account_value(program_response)["data"][0]))
        self.pd_key = (self.programdata, 0, 45)

    async def run_program(self):
        observations = await collect([address(*self.program)], replay_transport(self.responses))
        return by_metric(observations, "addr-raydium-amm")

    async def test_immutable_program_reports_zero_and_no_authority(self):
        self.responses[self.pd_key] = with_data(
            self.responses[self.pd_key], struct.pack("<IQ", 3, 400_000_000) + b"\x00" + bytes(32)
        )
        metrics = await self.run_program()
        self.assertEqual(metrics[METRIC_UPGRADEABLE].value_numeric, Decimal(0))
        self.assertNotIn(METRIC_AUTHORITY, metrics)
        self.assertEqual(metrics[METRIC_DEPLOY_SLOT].value_numeric, Decimal(400_000_000))

    async def test_truncated_programdata_is_error_never_a_guess(self):
        original = base64.b64decode(account_value(self.responses[self.pd_key])["data"][0])
        self.responses[self.pd_key] = with_data(self.responses[self.pd_key], original[:20])
        metrics = await self.run_program()
        self.assertIs(metrics[METRIC_OWNER].status, ObservationStatus.OK)
        for metric in (METRIC_UPGRADEABLE, METRIC_AUTHORITY, METRIC_DEPLOY_SLOT):
            self.assertIs(metrics[metric].status, ObservationStatus.ERROR)
            self.assertEqual(metrics[metric].error_code, "DECODE_TRUNCATED")

    async def test_programdata_with_wrong_owner_is_error(self):
        self.responses[self.pd_key] = with_data(
            self.responses[self.pd_key],
            base64.b64decode(account_value(self.responses[self.pd_key])["data"][0]),
            owner="11111111111111111111111111111111",
        )
        metrics = await self.run_program()
        self.assertEqual(metrics[METRIC_DEPLOY_SLOT].error_code, "PROGRAMDATA_OWNER_MISMATCH")

    async def test_legacy_loader_program_is_immutable_with_inapplicable_deploy_slot(self):
        key = (self.program[1], 0, 36)
        self.responses[key] = with_data(
            self.responses[key], b"\x7fELF" + bytes(32), owner="BPFLoader2111111111111111111111111111111111"
        )
        metrics = await self.run_program()
        self.assertEqual(metrics[METRIC_UPGRADEABLE].value_numeric, Decimal(0))
        self.assertIs(metrics[METRIC_DEPLOY_SLOT].status, ObservationStatus.MISSING)
        self.assertEqual(metrics[METRIC_DEPLOY_SLOT].error_code, "NOT_APPLICABLE_LEGACY_LOADER")


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_401_and_timeout_become_error_rows_for_every_metric(self):
        program = address(*REGISTRY[0])
        mint = address(*REGISTRY[8])
        transport = replay_transport(
            status_overrides={program.address: 401},
            raise_for={mint.address: httpx.ReadTimeout("timed out")},
        )
        observations = await collect([program, mint], transport)
        program_metrics = by_metric(observations, program.id)
        self.assertEqual(len(program_metrics), 4)
        self.assertTrue(all(o.error_code == "HTTP_401" for o in program_metrics.values()))
        self.assertEqual(by_metric(observations, mint.id)[METRIC_OWNER].error_code, "TIMEOUT")

    async def test_rpc_url_secret_never_reaches_logs_or_error_codes(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        root = logging.getLogger()
        httpx_logger = logging.getLogger("httpx")
        saved = (root.level, httpx_logger.level)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        httpx_logger.setLevel(logging.DEBUG)
        try:
            observations = await collect(
                [address(*REGISTRY[0])], replay_transport(status_overrides={REGISTRY[0][1]: 500})
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(saved[0])
            httpx_logger.setLevel(saved[1])
        self.assertNotIn("fixture-secret-key", stream.getvalue())
        for o in observations:
            self.assertNotIn("fixture-secret-key", o.error_code or "")
            self.assertNotIn("://", o.error_code or "")
            self.assertEqual(o.error_code, "HTTP_500")


class ObservationContractTests(unittest.TestCase):
    def test_missing_and_error_cannot_carry_values(self):
        with self.assertRaises(InvalidObservation):
            Observation("m", ObservationStatus.MISSING, "slot", "s", value_numeric=Decimal(0))
        with self.assertRaises(InvalidObservation):
            Observation("m", ObservationStatus.ERROR, "slot", "s", value_text="x", error_code="E")

    def test_ok_requires_value_and_error_requires_code(self):
        with self.assertRaises(InvalidObservation):
            Observation("m", ObservationStatus.OK, "slot", "s")
        with self.assertRaises(InvalidObservation):
            Observation("m", ObservationStatus.ERROR, "slot", "s")

    def test_error_code_must_be_an_identifier_not_free_text(self):
        with self.assertRaises(InvalidObservation):
            Observation.failed("m", "slot", "s", "https://rpc/?api-key=abc")

    def test_numeric_values_must_be_decimal(self):
        with self.assertRaises(InvalidObservation):
            Observation("m", ObservationStatus.OK, "slot", "s", value_numeric=1.5)


if __name__ == "__main__":
    unittest.main()
