import asyncio
import os
import unittest
import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
import httpx

from app.adapters.base import Observation, ObservationStatus
from app.adapters.onchain_security import OnchainSecurityAdapter
from app.solana.account_reader import AccountReader, PUBLIC_MAINNET_RPC_URL, resolve_security_rpc_url
from app.tasks import security_collector
from app.tasks.security_collector import (
    INSERT_OBSERVATION,
    RECENTLY_COLLECTED_QUERY,
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
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.open_tx += 1
        self.conn.events.append("begin")

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.open_tx -= 1
        self.conn.events.append("rollback" if exc_type else "commit")
        return False


class FakeConnection:
    """Records every statement and how many transactions are open at the time."""

    def __init__(self, rows, lock=True, recent=(), written_meanwhile=(), fail_insert_for=None, fetch_error=None):
        self.rows, self.lock = rows, lock
        self.recent = set(recent)  # protocol ids collected within the spacing, seen at plan time
        self.written_meanwhile = set(written_meanwhile)  # written by another instance after planning
        self.fail_insert_for = fail_insert_for
        self.fetch_error = fetch_error
        self.events, self.inserted, self.queries = [], [], []
        self.open_tx = 0

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, query, *args):
        self.queries.append((query, args, self.open_tx))
        if "pg_try_advisory_xact_lock" in query:
            return self.lock
        if "EXISTS" in query:
            return args[0] in self.written_meanwhile
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        self.queries.append((query, args, self.open_tx))

    async def fetch(self, query, *args):
        self.queries.append((query, args, self.open_tx))
        if self.fetch_error:
            raise self.fetch_error
        if query == RECENTLY_COLLECTED_QUERY:
            return [{"protocol_id": pid} for pid in self.recent]
        return self.rows

    async def executemany(self, query, rows):
        assert self.open_tx == 1, "observations must be written inside a transaction"
        self.events.append("insert")
        if self.fail_insert_for and any(r[1] == self.fail_insert_for for r in rows):
            raise RuntimeError("insert rejected")
        self.inserted.extend(rows)


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.held = 0

    def acquire(self):
        pool = self

        class Acquire:
            async def __aenter__(self):
                pool.held += 1
                return pool.conn

            async def __aexit__(self, *exc):
                pool.held -= 1
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
    async def run_collector(self, conn, factory=fixture_adapter, pool=None):
        with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value="https://rpc.test"):
            return await collect_security_observations_once(pool=pool or FakePool(conn), adapter_factory=factory)

    async def test_plan_uses_transaction_level_try_lock_inside_a_short_transaction(self):
        conn = FakeConnection(ROWS)
        report = await self.run_collector(conn)
        lock_query, lock_args, open_tx = conn.queries[0]
        self.assertIn("pg_try_advisory_xact_lock", lock_query)
        self.assertEqual(lock_args, (SECURITY_COLLECTOR_ADVISORY_LOCK_ID,))
        self.assertEqual(open_tx, 1)
        self.assertNotIn("pg_advisory_unlock", " ".join(q for q, _, _ in conn.queries))
        self.assertTrue(report.ran)
        self.assertEqual(report.protocols_collected, 2)

    async def test_no_transaction_or_connection_is_held_during_rpc(self):
        conn = FakeConnection(ROWS)
        pool = FakePool(conn)
        seen = []

        def watching(client, url):
            inner = fixture_adapter(client, url)

            class Watching:
                key, version = inner.key, inner.version

                async def collect(self, ctx):
                    seen.append((ctx.slug, conn.open_tx, pool.held))
                    return await inner.collect(ctx)

            return Watching()

        report = await self.run_collector(conn, factory=watching, pool=pool)
        self.assertEqual(report.protocols_collected, 2)
        self.assertEqual(sorted(seen), [("jito", 0, 0), ("orca", 0, 0)])

    async def test_each_protocol_is_written_in_its_own_locked_transaction_with_full_provenance(self):
        conn = FakeConnection(ROWS)
        report = await self.run_collector(conn)
        # plan transaction + one write transaction per protocol, all committed
        self.assertEqual(conn.events, ["begin", "commit", "begin", "insert", "commit", "begin", "insert", "commit"])
        blocking = [(q, a, t) for q, a, t in conn.queries if "pg_advisory_xact_lock" in q and "try" not in q]
        self.assertEqual(len(blocking), 2)
        self.assertTrue(all(t == 1 and a == (SECURITY_COLLECTOR_ADVISORY_LOCK_ID,) for _, a, t in blocking))
        self.assertEqual(report.observations_written, 5)  # 4 for the program + 1 owner for the pool
        for row in conn.inserted:
            (_id, protocol_id, address_id, metric, status, numeric, text, unit, source,
             observed_at, slot, collected_at, adapter_key, adapter_version, error_code) = row
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

    async def test_recently_collected_protocols_are_not_collected_again(self):
        calls = []

        def counting(client, url):
            return OnchainSecurityAdapter(AccountReader(url, httpx.AsyncClient(transport=replay_transport(calls=calls))))

        conn = FakeConnection(ROWS, recent={"p-orca"})
        report = await self.run_collector(conn, factory=counting)
        self.assertEqual(report.recently_collected, ["orca"])
        self.assertEqual(report.protocols_collected, 1)
        self.assertTrue(all(r[1] == "p-jito" for r in conn.inserted))
        self.assertNotIn("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", [c[0] for c in calls])  # no RPC for orca
        _, args, _ = next(x for x in conn.queries if x[0] == RECENTLY_COLLECTED_QUERY)
        self.assertEqual(args[0], "onchain_security")
        self.assertIsNone(args[1].tzinfo)

    async def test_everything_recently_collected_skips_the_run(self):
        conn = FakeConnection(ROWS, recent={"p-orca", "p-jito"})
        report = await self.run_collector(conn)
        self.assertFalse(report.ran)
        self.assertEqual(report.skipped_reason, "recently_collected")
        self.assertEqual(conn.inserted, [])

    async def test_another_instance_writing_meanwhile_prevents_duplicate_rows(self):
        conn = FakeConnection(ROWS, written_meanwhile={"p-orca"})
        report = await self.run_collector(conn)
        self.assertIn("orca", report.recently_collected)
        self.assertEqual(report.protocols_collected, 1)
        self.assertTrue(all(r[1] == "p-jito" for r in conn.inserted))

    async def test_one_protocol_failing_to_write_does_not_lose_the_other(self):
        conn = FakeConnection(ROWS, fail_insert_for="p-orca")
        report = await self.run_collector(conn)
        self.assertEqual(report.failed_protocols, ["orca"])
        self.assertEqual(report.protocols_collected, 1)
        self.assertEqual(conn.events.count("rollback"), 1)
        self.assertTrue(all(r[1] == "p-jito" for r in conn.inserted))
        self.assertEqual(conn.open_tx, 0)

    async def test_adapter_crash_writes_nothing_for_that_protocol_only(self):
        def half_broken(client, url):
            inner = fixture_adapter(client, url)

            class HalfBroken:
                key, version = inner.key, inner.version

                async def collect(self, ctx):
                    if ctx.slug == "jito":
                        raise RuntimeError("bug")
                    return await inner.collect(ctx)

            return HalfBroken()

        conn = FakeConnection(ROWS)
        report = await self.run_collector(conn, factory=half_broken)
        self.assertEqual(report.failed_protocols, ["jito"])
        self.assertEqual(report.protocols_collected, 1)
        self.assertTrue(conn.inserted)
        self.assertTrue(all(r[1] == "p-orca" for r in conn.inserted))

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


class CollectorStatusTests(unittest.TestCase):
    def test_endpoint_is_described_by_host_only(self):
        configured, host, public = security_collector._describe_endpoint("https://mainnet.helius-rpc.com/?api-key=fixture-secret-key")
        self.assertEqual((configured, host, public), (True, "mainnet.helius-rpc.com", False))
        self.assertNotIn("fixture-secret-key", repr(security_collector._describe_endpoint("https://x.test/?api-key=fixture-secret-key")))
        self.assertEqual(security_collector._describe_endpoint(PUBLIC_MAINNET_RPC_URL)[2], True)
        self.assertEqual(security_collector._describe_endpoint(None), (False, None, False))

    def test_status_is_serialisable_and_has_no_url(self):
        with patch("app.tasks.security_collector.resolve_security_rpc_url",
                   return_value="https://mainnet.helius-rpc.com/?api-key=fixture-secret-key"):
            status = security_collector.collector_status()
        self.assertIn("interval_seconds", status)
        self.assertNotIn("rpc_url", status)
        self.assertEqual(status["rpc_host"], "mainnet.helius-rpc.com")
        self.assertNotIn("fixture-secret-key", repr(status))


class RunOutcomeTests(unittest.TestCase):
    def report(self, **kw):
        return security_collector.CollectorReport(**{"ran": True, **kw})

    def test_outcomes(self):
        outcome = security_collector.run_outcome
        self.assertEqual(outcome(self.report(protocols_collected=6)), "succeeded")
        self.assertEqual(outcome(self.report(protocols_collected=5, failed_protocols=["orca"])), "partial")
        self.assertEqual(outcome(self.report(failed_protocols=["orca", "jito"])), "failed")
        # every due protocol was written by another instance meanwhile
        self.assertEqual(outcome(self.report(recently_collected=["orca"])), "recently_collected")
        for reason in ("locked", "recently_collected", "schema_missing", "no_rpc_endpoint"):
            self.assertEqual(outcome(self.report(ran=False, skipped_reason=reason)), reason)


class CollectorStateTests(unittest.TestCase):
    def status(self, **kw):
        return security_collector.CollectorStatus(**{"enabled": True, "interval_seconds": 900, **kw})

    def test_disabled_is_reported_as_disabled_not_missing_rpc(self):
        state = security_collector.collector_state
        self.assertEqual(state(self.status(enabled=False), rpc_configured=False), "disabled")
        self.assertEqual(state(self.status(enabled=False), rpc_configured=True), "disabled")
        self.assertEqual(state(self.status(), rpc_configured=False), "no_rpc_endpoint")

    def test_lifecycle_states(self):
        state = security_collector.collector_state
        started = datetime(2026, 11, 12)
        self.assertEqual(state(self.status(), True), "starting")
        self.assertEqual(state(self.status(last_started_at=started, last_outcome="succeeded"), True), "active")
        self.assertEqual(state(self.status(last_started_at=started, last_outcome="partial"), True), "active")
        self.assertEqual(state(self.status(last_started_at=started, last_outcome="failed"), True), "failing")
        self.assertEqual(state(self.status(last_started_at=started, last_outcome="error"), True), "failing")


class CollectorLoopTests(unittest.IsolatedAsyncioTestCase):
    """One loop iteration at a time: the sleep raises CancelledError to stop it."""

    async def asyncSetUp(self):
        self.status = security_collector.CollectorStatus(enabled=True, interval_seconds=900)
        patcher = patch.object(security_collector, "_status", self.status)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def iterate(self, result):
        collect = AsyncMock(side_effect=[result]) if isinstance(result, BaseException) else AsyncMock(return_value=result)
        with (
            patch.object(security_collector, "collect_security_observations_once", collect),
            patch.object(security_collector.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with self.assertRaises(asyncio.CancelledError):  # cancellation still propagates
                await security_collector.security_collector_loop()

    async def test_fully_failed_run_is_not_reported_as_ran(self):
        await self.iterate(security_collector.CollectorReport(ran=True, failed_protocols=["orca", "jito"]))
        self.assertEqual(self.status.last_outcome, "failed")
        self.assertEqual(self.status.last_report["failed_protocols"], ["orca", "jito"])
        self.assertEqual(self.status.consecutive_failures, 1)
        self.assertIsNotNone(self.status.last_finished_at)
        self.assertIsNotNone(self.status.next_run_at)

    async def test_partial_run_keeps_per_protocol_failures(self):
        await self.iterate(security_collector.CollectorReport(ran=True, protocols_collected=5, observations_written=33, failed_protocols=["orca"]))
        self.assertEqual(self.status.last_outcome, "partial")
        self.assertEqual(self.status.last_report["failed_protocols"], ["orca"])
        self.assertEqual(self.status.consecutive_failures, 0)

    async def test_failures_accumulate_and_reset(self):
        await self.iterate(RuntimeError("db down"))
        self.assertEqual((self.status.last_outcome, self.status.last_report), ("error", {"error": "RuntimeError"}))
        await self.iterate(security_collector.CollectorReport(ran=True, failed_protocols=["orca"]))
        self.assertEqual(self.status.consecutive_failures, 2)
        await self.iterate(security_collector.CollectorReport(ran=True, protocols_collected=6))
        self.assertEqual((self.status.last_outcome, self.status.consecutive_failures), ("succeeded", 0))

    async def test_cancellation_during_a_run_is_not_recorded_as_failure(self):
        await self.iterate(asyncio.CancelledError())
        self.assertIsNone(self.status.last_outcome)
        self.assertEqual(self.status.consecutive_failures, 0)


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


@unittest.skipUnless(
    os.environ.get("SOLANAGUARD_TEST_DATABASE_URL"),
    "set SOLANAGUARD_TEST_DATABASE_URL to a disposable, migrated and seeded database to run",
)
class CollectorDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Real PostgreSQL: the planning/recheck SQL and concurrent-run dedupe.

    Each test uses its own adapter key, so it never deletes rows and never
    interacts with observations written by the real collector.
    """

    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["SOLANAGUARD_TEST_DATABASE_URL"], min_size=1, max_size=4)
        self.key = f"test-{uuid.uuid4().hex[:12]}"

    async def asyncTearDown(self):
        await self.pool.close()

    def factory(self, client, url):
        inner = fixture_adapter(client, url)
        key = self.key

        class Keyed:
            version = inner.version

            async def collect(self, ctx):
                return await inner.collect(ctx)

        Keyed.key = key
        return Keyed()

    async def run_once(self):
        with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value="https://rpc.test"):
            return await collect_security_observations_once(pool=self.pool, adapter_factory=self.factory)

    async def count(self):
        return await self.pool.fetchval('SELECT count(*) FROM metric_observations WHERE "adapterKey" = $1', self.key)

    async def test_second_run_within_spacing_is_skipped(self):
        first = await self.run_once()
        self.assertTrue(first.ran)
        self.assertGreater(first.observations_written, 0)
        self.assertEqual(await self.count(), first.observations_written)
        second = await self.run_once()
        self.assertFalse(second.ran)
        self.assertEqual(second.skipped_reason, "recently_collected")
        self.assertEqual(await self.count(), first.observations_written)

    async def test_concurrent_runs_never_write_a_protocol_twice(self):
        """Both runs plan before either writes, so both collect every protocol;
        the second run's persist-time recheck must then reject every write.

        Ordering is forced with events, not sleeps:
        A plans and blocks in its first collect until B has planned and is
        collecting too; B then blocks until A has written everything.
        """
        a_collecting, b_collecting, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def gated(on_first_collect):
            def factory(client, url):
                inner = self.factory(client, url)
                first = [True]

                class Gated:
                    key, version = inner.key, inner.version

                    async def collect(self, ctx):
                        if first[0]:
                            first[0] = False
                            await on_first_collect()
                        return await inner.collect(ctx)

                return Gated()
            return factory

        async def a_gate():
            a_collecting.set()
            await asyncio.wait_for(b_collecting.wait(), 30)

        async def b_gate():
            b_collecting.set()
            await asyncio.wait_for(a_done.wait(), 30)

        async def run(factory):
            with patch("app.tasks.security_collector.resolve_security_rpc_url", return_value="https://rpc.test"):
                return await collect_security_observations_once(pool=self.pool, adapter_factory=factory)

        async def run_a():
            try:
                return await run(gated(a_gate))
            finally:
                a_done.set()

        async def run_b():
            await asyncio.wait_for(a_collecting.wait(), 30)  # A's plan transaction has committed
            return await run(gated(b_gate))

        a, b = await asyncio.wait_for(asyncio.gather(run_a(), run_b()), 120)

        # Both genuinely ran: B was not skipped at planning, and collected over RPC.
        self.assertTrue(a.ran and b.ran)
        self.assertIsNone(b.skipped_reason)
        self.assertGreater(a.protocols_collected, 0)
        self.assertEqual(a.failed_protocols, [])
        # Every one of B's writes was rejected by the recheck.
        self.assertEqual(b.protocols_collected, 0)
        self.assertEqual(b.observations_written, 0)
        self.assertEqual(sorted(b.recently_collected), sorted(set(b.recently_collected)))
        self.assertEqual(len(b.recently_collected), a.protocols_collected)
        self.assertEqual(security_collector.run_outcome(b), "recently_collected")
        # The database holds exactly A's rows, one batch per protocol.
        self.assertEqual(await self.count(), a.observations_written)
        per_protocol = await self.pool.fetch(
            'SELECT "protocolId", count(DISTINCT "collectedAt") AS batches FROM metric_observations '
            'WHERE "adapterKey" = $1 GROUP BY 1', self.key)
        self.assertEqual(len(per_protocol), a.protocols_collected)
        self.assertTrue(all(r["batches"] == 1 for r in per_protocol), per_protocol)


if __name__ == "__main__":
    unittest.main()
