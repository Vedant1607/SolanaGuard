import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from app.tasks import ingest


class FakeLockConnection:
    def __init__(self, lock_available=True, lock_error=None, unlock_error=None):
        self.lock_available = lock_available
        self.lock_error = lock_error
        self.unlock_error = unlock_error
        self.queries = []

    async def fetchval(self, query, lock_id):
        self.queries.append((query, lock_id))
        if "pg_try_advisory_lock" in query:
            if self.lock_error:
                raise self.lock_error
            return self.lock_available
        if self.unlock_error:
            raise self.unlock_error
        return True


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection
        self.entered = False
        self.exited = False
        self.exit_exception = None

    async def __aenter__(self):
        self.entered = True
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True
        self.exit_exception = exc
        return None


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.acquires = []

    def acquire(self):
        acquire = FakeAcquire(self.connection)
        self.acquires.append(acquire)
        return acquire


class IngestionCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_acquired_lock_runs_ingestion_and_releases_it(self):
        connection = FakeLockConnection()
        pool = FakePool(connection)
        run_ingestion = AsyncMock(return_value=6)

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
        ):
            self.assertEqual(await ingest.ingest_all_protocols(), 6)

        run_ingestion.assert_awaited_once()
        self.assertEqual(len(connection.queries), 2)
        self.assertIn("pg_try_advisory_lock", connection.queries[0][0])
        self.assertIn("pg_advisory_unlock", connection.queries[1][0])
        self.assertNotEqual(ingest.INGESTION_ADVISORY_LOCK_ID, 8_191_424_271)
        self.assertTrue(pool.acquires[0].entered)
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsNone(pool.acquires[0].exit_exception)

    async def test_lock_contention_skips_cycle_without_running_ingestion(self):
        connection = FakeLockConnection(lock_available=False)
        pool = FakePool(connection)
        run_ingestion = AsyncMock()

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
        ):
            self.assertEqual(await ingest.ingest_all_protocols(), 0)

        run_ingestion.assert_not_awaited()
        self.assertEqual(len(connection.queries), 1)
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsNone(pool.acquires[0].exit_exception)

    async def test_ingestion_error_releases_lock_before_propagating(self):
        connection = FakeLockConnection()
        pool = FakePool(connection)
        run_ingestion = AsyncMock(side_effect=RuntimeError("ingestion failed"))

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
        ):
            with self.assertRaisesRegex(RuntimeError, "ingestion failed"):
                await ingest.ingest_all_protocols()

        self.assertEqual(len(connection.queries), 2)
        self.assertIn("pg_advisory_unlock", connection.queries[1][0])
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsInstance(pool.acquires[0].exit_exception, RuntimeError)

    async def test_cancellation_releases_lock_before_propagating(self):
        connection = FakeLockConnection()
        pool = FakePool(connection)
        run_ingestion = AsyncMock(side_effect=asyncio.CancelledError)

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await ingest.ingest_all_protocols()

        self.assertEqual(len(connection.queries), 2)
        self.assertIn("pg_advisory_unlock", connection.queries[1][0])
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsInstance(pool.acquires[0].exit_exception, asyncio.CancelledError)

    async def test_lock_acquisition_error_returns_connection_and_propagates(self):
        connection = FakeLockConnection(lock_error=RuntimeError("lock query failed"))
        pool = FakePool(connection)
        run_ingestion = AsyncMock()

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
        ):
            with self.assertRaisesRegex(RuntimeError, "lock query failed"):
                await ingest.ingest_all_protocols()

        run_ingestion.assert_not_awaited()
        self.assertEqual(len(connection.queries), 1)
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsInstance(pool.acquires[0].exit_exception, RuntimeError)

    async def test_unlock_error_is_logged_and_connection_is_returned(self):
        connection = FakeLockConnection(unlock_error=RuntimeError("unlock query failed"))
        pool = FakePool(connection)
        run_ingestion = AsyncMock(return_value=6)

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", run_ingestion),
            self.assertLogs("app.tasks.ingest", level="ERROR") as logs,
        ):
            self.assertEqual(await ingest.ingest_all_protocols(), 6)

        self.assertIn("Failed to release ingestion advisory lock", "\n".join(logs.output))
        self.assertTrue(pool.acquires[0].exited)
        self.assertIsNone(pool.acquires[0].exit_exception)

    async def test_local_lock_skips_a_concurrent_invocation(self):
        connection = FakeLockConnection()
        pool = FakePool(connection)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_ingestion(_pool):
            started.set()
            await release.wait()
            return 6

        with (
            patch("app.tasks.ingest.get_pool", AsyncMock(return_value=pool)),
            patch("app.tasks.ingest._ingest_protocols", blocking_ingestion),
        ):
            first_run = asyncio.create_task(ingest.ingest_all_protocols())
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(await ingest.ingest_all_protocols(), 0)
            release.set()
            self.assertEqual(await first_run, 6)

        self.assertEqual(len(pool.acquires), 1)
        self.assertTrue(pool.acquires[0].exited)
