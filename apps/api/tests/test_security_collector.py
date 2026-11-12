import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
import httpx

from app.adapters.base import Observation, ObservationStatus
from app.adapters.onchain_security import OnchainSecurityAdapter
from app.solana.account_reader import AccountReader, PUBLIC_MAINNET_RPC_URL, resolve_security_rpc_url
from app.tasks import security_collector
from app.tasks.security_collector import (
    INSERT_OBSERVATION,
    REGISTRY_QUERY,
    SECURITY_COLLECTOR_ADVISORY_LOCK_ID,
    collect_security_observations_once,
)
from tests.solana_fixtures import replay_transport


def registry_row(protocol_id, slug, address_id, address, kind="PROGRAM", owner="BPFLoaderUpgradeab1e11111111111111111111111"):
    return {
        "protocol_id": protocol_id, "slug": slug, "address_id": address_id, "address": address,
        "kind": kind, "label": address_id, "is_shared_dependency": False, "expected_owner": owner,
    }


class FakeTransaction:
    def __init__(self, conn, savepoint):
        self.conn, self.savepoint = conn, savepoint

    async def __aenter__(self):
        self.conn.events.append("savepoint" if self.savepoint else "begin")

    async def __aexit__(self, exc_type, exc, tb):
        kind = "savepoint" if self.savepoint else "tx"
        self.conn.events.append(f"rollback-{kind}" if exc_type else f"commit-{kind}")
        return False


class FakeConnection:
    def __init__(self, rows, lock=True, fail_insert_for=None, fetch_error=None):
        self.rows, self.lock = rows, lock
        self.fail_insert_for = fail_insert_for
        self.fetch_error = fetch_error
        self.events, self.inserted, self.queries = [], [], []
        self.depth = 0

    def transaction(self):
        self.depth += 1
        return FakeTransaction(self, savepoint=self.depth > 1)

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return self.lock

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        if self.fetch_error:
            raise self.fetch_error
        return self.rows

    async def executemany(self, query, rows):
        if self.fail_insert_for and any(r[1] == self.fail_insert_for for r in rows):
            raise RuntimeError("insert rejected")
        self.inserted.extend(rows)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Acquire()


def fixture_adapter(client, url):
    replay = httpx.AsyncClient(transport=replay_transport())
    return OnchainSecurityAdapter(AccountReader(url, replay))


ROWS = [
    registry_row("p-orca", "orca", "a-orca", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
    registry_row("p-jito", "jito", "a-jito", "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb", "STAKE_POOL",
                 "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy"),
]


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def run_collector(self, conn):
        with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value="https://rpc.test"):
            return await collect_security_observations_once(pool=FakePool(conn), adapter_factory=fixture_adapter)

    async def test_uses_transaction_level_advisory_lock_inside_a_transaction(self):
        conn = FakeConnection(ROWS)
        report = await self.run_collector(conn)
        lock_query, lock_args = conn.queries[0]
        self.assertIn("pg_try_advisory_xact_lock", lock_query)
        self.assertNotIn("pg_advisory_unlock", " ".join(q for q, _ in conn.queries))
        self.assertEqual(lock_args, (SECURITY_COLLECTOR_ADVISORY_LOCK_ID,))
        self.assertEqual(conn.events[0], "begin")
        self.assertEqual(conn.events[-1], "commit-tx")
        self.assertTrue(report.ran)
        self.assertEqual(report.protocols_collected, 2)

    async def test_writes_one_savepoint_per_protocol_with_full_provenance(self):
        conn = FakeConnection(ROWS)
        report = await self.run_collector(conn)
        self.assertEqual(conn.events.count("savepoint"), 2)
        self.assertEqual(report.observations_written, 5)  # 4 for the program + 1 owner for the pool
        for row in conn.inserted:
            (_id, protocol_id, address_id, metric, status, numeric, text, unit, source,
             observed_at, slot, collected_at, adapter_key, adapter_version, error_code) = row
            self.assertIn(protocol_id, ("p-orca", "p-jito"))
            self.assertEqual({"p-orca": "a-orca", "p-jito": "a-jito"}[protocol_id], address_id)
            self.assertEqual((source, adapter_key, adapter_version), ("solana-rpc", "onchain_security", "1"))
            self.assertIsInstance(collected_at, datetime)
            self.assertIsNone(collected_at.tzinfo)  # stored as naive UTC like the rest of the schema
            self.assertIsInstance(slot, int)
            self.assertEqual(status, "OK")

    async def test_registry_query_only_selects_active_protocols_and_addresses(self):
        self.assertIn('p."isActive" = true', REGISTRY_QUERY)
        self.assertIn('a."isActive" = true', REGISTRY_QUERY)
        self.assertIn('$5::"ObservationStatus"', INSERT_OBSERVATION)

    async def test_lock_held_elsewhere_skips_without_reading_registry(self):
        conn = FakeConnection(ROWS, lock=False)
        report = await self.run_collector(conn)
        self.assertFalse(report.ran)
        self.assertEqual(report.skipped_reason, "locked")
        self.assertEqual(len(conn.queries), 1)
        self.assertEqual(conn.inserted, [])

    async def test_one_protocol_failing_to_write_does_not_lose_the_other(self):
        conn = FakeConnection(ROWS, fail_insert_for="p-orca")
        report = await self.run_collector(conn)
        self.assertEqual(report.failed_protocols, ["orca"])
        self.assertEqual(report.protocols_collected, 1)
        self.assertIn("rollback-savepoint", conn.events)
        self.assertTrue(all(r[1] == "p-jito" for r in conn.inserted))

    async def test_adapter_crash_writes_nothing_for_that_protocol(self):
        class Exploding:
            key, version = "x", "1"

            async def collect(self, ctx):
                raise RuntimeError("bug")

        conn = FakeConnection(ROWS)
        with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value="https://rpc.test"):
            report = await collect_security_observations_once(
                pool=FakePool(conn), adapter_factory=lambda client, url: Exploding()
            )
        self.assertEqual(sorted(report.failed_protocols), ["jito", "orca"])
        self.assertEqual(conn.inserted, [])

    async def test_missing_schema_is_reported_not_raised(self):
        conn = FakeConnection(ROWS, fetch_error=asyncpg.exceptions.UndefinedTableError("no table"))
        report = await self.run_collector(conn)
        self.assertEqual(report.skipped_reason, "schema_missing")

    async def test_no_rpc_endpoint_keeps_collector_idle(self):
        conn = FakeConnection(ROWS)
        with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value=None):
            report = await collect_security_observations_once(pool=FakePool(conn), adapter_factory=fixture_adapter)
        self.assertEqual(report.skipped_reason, "no_rpc_endpoint")
        self.assertEqual(conn.queries, [])


class RpcEndpointResolutionTests(unittest.TestCase):
    def test_configured_url_wins(self):
        with patch("app.solana.account_reader.settings.SOLANA_RPC_URL", "https://provider.test/x"):
            self.assertEqual(resolve_security_rpc_url(), "https://provider.test/x")

    def test_public_endpoint_only_in_debug(self):
        with patch("app.solana.account_reader.settings.SOLANA_RPC_URL", ""):
            with patch("app.solana.account_reader.settings.DEBUG", True):
                self.assertEqual(resolve_security_rpc_url(), PUBLIC_MAINNET_RPC_URL)
            with patch("app.solana.account_reader.settings.DEBUG", False):
                self.assertIsNone(resolve_security_rpc_url())


class ObservationRowTests(unittest.TestCase):
    def test_error_rows_have_no_value(self):
        row = security_collector.observation_row(
            Observation.failed("security.last_deploy_slot", "slot", "solana-rpc", "TIMEOUT", address_id="a"),
            "p", datetime(2026, 1, 1), "onchain_security", "1",
        )
        self.assertEqual(row[4], "ERROR")
        self.assertIsNone(row[5])
        self.assertIsNone(row[6])
        self.assertEqual(row[14], "TIMEOUT")

    def test_numeric_values_stay_decimal(self):
        row = security_collector.observation_row(
            Observation("security.last_deploy_slot", ObservationStatus.OK, "slot", "solana-rpc",
                        value_numeric=Decimal(445_763_204), address_id="a", source_slot=1),
            "p", datetime(2026, 1, 1), "onchain_security", "1",
        )
        self.assertEqual(row[5], Decimal(445_763_204))


if __name__ == "__main__":
    unittest.main()
