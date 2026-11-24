import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from fastapi import FastAPI

from app.main import lifespan
from app.tasks import anomaly_detection


class FakeConnection:
    def __init__(self, lock_available=True):
        self.lock_available = lock_available
        self.queries = []

    async def fetchval(self, query, lock_id):
        self.queries.append((query, lock_id))
        if "pg_try_advisory_lock" in query:
            return self.lock_available
        return True


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


def detector_report():
    return SimpleNamespace(
        snapshots_processed=42,
        anomalies_inserted=3,
        duplicate_anomalies_skipped=1,
        persistence_failures=0,
    )


class ScheduledDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_run_uses_lock_and_logs_report_statistics(self):
        connection = FakeConnection()

        with (
            patch("app.tasks.anomaly_detection.get_pool", AsyncMock(return_value=FakePool(connection))),
            patch("app.tasks.anomaly_detection.run_detector", AsyncMock(return_value=detector_report())) as run,
            self.assertLogs("app.tasks.anomaly_detection", level="INFO") as logs,
        ):
            self.assertTrue(await anomaly_detection.run_scheduled_detector_once())

        run.assert_awaited_once()
        self.assertEqual(len(connection.queries), 2)
        self.assertIn("pg_try_advisory_lock", connection.queries[0][0])
        self.assertIn("pg_advisory_unlock", connection.queries[1][0])
        self.assertIn("snapshots_processed=42", "\n".join(logs.output))
        self.assertIn("duplicate_records_skipped=1", "\n".join(logs.output))

    async def test_lock_contention_skips_detector_run(self):
        connection = FakeConnection(lock_available=False)

        with (
            patch("app.tasks.anomaly_detection.get_pool", AsyncMock(return_value=FakePool(connection))),
            patch("app.tasks.anomaly_detection.run_detector", AsyncMock()) as run,
        ):
            self.assertFalse(await anomaly_detection.run_scheduled_detector_once())

        run.assert_not_awaited()
        self.assertEqual(len(connection.queries), 1)

    async def test_detector_exception_does_not_stop_next_scheduled_cycle(self):
        connection = FakeConnection()
        runs = AsyncMock(side_effect=[RuntimeError("detector failed"), detector_report()])
        sleeps = 0

        async def stop_after_second_sleep(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise asyncio.CancelledError

        with (
            patch("app.tasks.anomaly_detection.get_pool", AsyncMock(return_value=FakePool(connection))),
            patch("app.tasks.anomaly_detection.run_detector", runs),
            patch("app.tasks.anomaly_detection.asyncio.sleep", stop_after_second_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await anomaly_detection.anomaly_detector_loop()

        self.assertEqual(runs.await_count, 2)
        self.assertEqual(sleeps, 2)

    async def test_lifespan_runs_independent_loops_and_cancels_them_before_pool_close(self):
        ingestion_started = asyncio.Event()
        detector_started = asyncio.Event()
        cancellations = []

        async def ingestion_loop():
            ingestion_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append("ingestion")
                raise

        async def detector_loop():
            detector_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append("detector")
                raise

        close_pool = AsyncMock()
        with (
            patch("app.main._ingestion_loop", ingestion_loop),
            patch("app.main.anomaly_detector_loop", detector_loop),
            patch("app.main.close_pool", close_pool),
        ):
            async with lifespan(FastAPI()):
                await asyncio.wait_for(ingestion_started.wait(), timeout=1)
                await asyncio.wait_for(detector_started.wait(), timeout=1)
                close_pool.assert_not_awaited()

        self.assertCountEqual(cancellations, ["ingestion", "detector"])
        close_pool.assert_awaited_once()
