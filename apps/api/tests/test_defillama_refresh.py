"""DefiLlama history refresh scheduler and status. Fakes only: nothing touches a real database."""

import asyncio
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import history
from app.tasks import defillama_refresh as dr

SID = "7672848120509820967"


class FakeConn:
    def __init__(self, db="solanaguard", sid=SID, lock_free=True, identity_error=None, log=None):
        self.db, self.sid, self.lock_free, self.identity_error, self.log = db, sid, lock_free, identity_error, log if log is not None else []

    async def fetchrow(self, query, *args):
        self.log.append("identity")
        if self.identity_error:
            raise self.identity_error
        return {"database": self.db, "system_identifier": self.sid}

    async def fetchval(self, query, *args):
        if "pg_try_advisory_lock" in query:
            self.log.append("try_lock")
            return self.lock_free
        self.log.append("unlock")
        return True


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Ctx()


def report(protocols):
    """protocols: [(slug, error, verified, last_day)]."""
    ps = [SimpleNamespace(slug=s, error=e, verified=v, last_day=d) for s, e, v, d in protocols]
    return SimpleNamespace(protocols=ps, totals={"inserted": 3, "already_stored": 10, "conflicts": 0 if all(v or e for _, e, v, _ in protocols) else 1})


def job(log, name, result=None, error=None, gate=None):
    async def run(*, pool):
        log.append(f"{name}:start")
        if gate is not None:
            await gate.wait()
        log.append(f"{name}:end")
        if error:
            raise error
        return result or report([("raydium", None, True, "2026-11-13")])
    return run


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.object(settings, k, v) for k, v in {
            "DEFILLAMA_HISTORY_REFRESH_ENABLED": True,
            "DEFILLAMA_HISTORY_REFRESH_EXPECTED_DATABASE": "solanaguard",
            "DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER": SID,
        }.items()]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        dr.reset_status()
        self.log = []


class DefaultsTests(unittest.TestCase):
    def test_disabled_by_default_and_no_expected_identity(self):
        fresh = type(settings)(DATABASE_URL="postgresql://x@localhost/x", _env_file=None)
        self.assertFalse(fresh.DEFILLAMA_HISTORY_REFRESH_ENABLED)
        self.assertEqual(fresh.DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER, "")
        self.assertEqual((fresh.DEFILLAMA_HISTORY_REFRESH_INTERVAL_SECONDS, fresh.DEFILLAMA_HISTORY_REFRESH_STARTUP_DELAY_SECONDS), (21_600, 60))

    def test_lifespan_starts_the_loop_only_when_enabled(self):
        from app import main

        started = []

        async def fake_loop():
            started.append(True)
            await asyncio.sleep(3600)

        async def idle():
            await asyncio.sleep(3600)

        async def run_lifespan():
            async with main.lifespan(main.app):
                await asyncio.sleep(0)

        for enabled, expected in ((False, []), (True, [True])):
            started.clear()
            with patch.object(settings, "DEFILLAMA_HISTORY_REFRESH_ENABLED", enabled), \
                 patch.object(settings, "SECURITY_COLLECTOR_ENABLED", False), \
                 patch.object(main, "defillama_refresh_loop", fake_loop), \
                 patch.object(main, "_ingestion_loop", idle), \
                 patch.object(main, "anomaly_detector_loop", idle), \
                 patch.object(main, "close_pool", lambda: asyncio.sleep(0)):
                asyncio.run(run_lifespan())
            self.assertEqual(started, expected, enabled)


class CycleTests(Base):
    async def test_jobs_run_sequentially_after_identity_and_lock(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(log=self.log)), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "succeeded")
        self.assertEqual(self.log, ["identity", "try_lock", "tvl:start", "tvl:end", "tokens:start", "tokens:end", "unlock"])
        s = dr.refresh_status()
        self.assertEqual((s["last_outcome"], s["running"], s["consecutive_failures"]), ("succeeded", False, 0))
        self.assertIsNotNone(s["last_success_at"])
        self.assertEqual(s["jobs"]["tvl_history"]["latest_source_day"], "2026-11-13")
        self.assertTrue(s["identity"]["verified"])

    async def test_failed_tvl_job_still_attempts_tokens_and_reports_partial(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(log=self.log)), tvl_job=job(self.log, "tvl", error=RuntimeError("boom http://secret")),
                                         token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "partial")
        self.assertIn("tokens:start", self.log)
        s = dr.refresh_status()
        self.assertEqual(s["jobs"]["tvl_history"]["outcome"], "failed")
        self.assertEqual(s["jobs"]["tvl_history"]["error"], "RuntimeError")  # type only: no message, no URL
        self.assertNotIn("secret", str(s))
        self.assertIsNone(s["last_success_at"])
        self.assertIn("tvl_history: failed", s["last_failure"])

    async def test_both_failing_is_failure(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn()), tvl_job=job(self.log, "tvl", error=ValueError()),
                                         token_job=job(self.log, "tokens", error=ValueError()))
        self.assertEqual(outcome, "failed")
        self.assertEqual(dr.refresh_status()["consecutive_failures"], 1)

    async def test_protocol_failures_and_conflicts_are_partial_not_success(self):
        partial = report([("raydium", None, True, "2026-11-13"), ("orca", "HTTP_429", False, None)])
        conflict = report([("kamino", None, False, "2026-11-13")])
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn()), tvl_job=job(self.log, "tvl", result=partial), token_job=job(self.log, "tokens", result=conflict))
        self.assertEqual(outcome, "partial")
        s = dr.refresh_status()
        self.assertEqual(s["jobs"]["tvl_history"]["failed_protocols"], ["orca"])
        self.assertEqual(s["jobs"]["token_history"]["outcome"], "failed")  # no protocol verified
        self.assertEqual(s["jobs"]["token_history"]["unverified_protocols"], ["kamino"])

    async def test_wrong_database_runs_nothing(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(db="neondb", log=self.log)), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "skipped_identity")
        self.assertEqual(self.log, ["identity"])
        self.assertEqual(dr.refresh_status()["identity"]["reason"], "database_mismatch")

    async def test_wrong_system_identifier_runs_nothing(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(sid="1", log=self.log)), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "skipped_identity")
        self.assertEqual(self.log, ["identity"])
        s = dr.refresh_status()
        self.assertEqual(s["identity"]["reason"], "system_identifier_mismatch")
        self.assertIsNone(s["last_success_at"])
        self.assertIn("no backfill was run", s["last_failure"])

    async def test_unverifiable_identity_fails_closed(self):
        conn = FakeConn(identity_error=PermissionError("permission denied for function pg_control_system"), log=self.log)
        outcome = await dr.refresh_cycle(pool=FakePool(conn), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "skipped_identity")
        self.assertEqual(dr.refresh_status()["identity"]["reason"], "identity_check_failed:PermissionError")
        self.assertEqual(self.log, ["identity"])

    async def test_missing_expected_identifier_fails_closed(self):
        with patch.object(settings, "DEFILLAMA_HISTORY_REFRESH_EXPECTED_SYSTEM_IDENTIFIER", ""):
            outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(log=self.log)), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "skipped_identity")
        self.assertEqual(self.log, [])  # not even queried
        self.assertEqual(dr.refresh_status()["identity"]["reason"], "expected_identity_not_configured")

    async def test_refresh_lock_held_elsewhere_is_skipped_not_success(self):
        outcome = await dr.refresh_cycle(pool=FakePool(FakeConn(lock_free=False, log=self.log)), tvl_job=job(self.log, "tvl"), token_job=job(self.log, "tokens"))
        self.assertEqual(outcome, "skipped_locked")
        self.assertNotIn("tvl:start", self.log)
        s = dr.refresh_status()
        self.assertEqual(s["last_outcome"], "skipped_locked")
        self.assertIsNone(s["last_success_at"])

    async def test_overlapping_cycles_are_prevented_and_status_tracks_running(self):
        gate = asyncio.Event()
        first = asyncio.create_task(dr.refresh_cycle(pool=FakePool(FakeConn()), tvl_job=job(self.log, "tvl", gate=gate), token_job=job(self.log, "tokens")))
        while "tvl:start" not in self.log:
            await asyncio.sleep(0)
        self.assertTrue(dr.refresh_status()["running"])
        self.assertEqual(dr.refresh_status()["state"], "running")
        second = await dr.refresh_cycle(pool=FakePool(FakeConn()), tvl_job=job(self.log, "tvl2"), token_job=job(self.log, "tokens2"))
        self.assertEqual(second, "skipped_running")
        self.assertNotIn("tvl2:start", self.log)
        gate.set()
        self.assertEqual(await first, "succeeded")
        self.assertFalse(dr.refresh_status()["running"])

    async def test_cancellation_mid_cycle_propagates_and_releases(self):
        gate = asyncio.Event()
        task = asyncio.create_task(dr.refresh_cycle(pool=FakePool(FakeConn(log=self.log)), tvl_job=job(self.log, "tvl", gate=gate), token_job=job(self.log, "tokens")))
        while "tvl:start" not in self.log:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        s = dr.refresh_status()
        self.assertEqual((s["last_outcome"], s["running"]), ("cancelled", False))
        self.assertIn("unlock", self.log)
        self.assertNotIn("tokens:start", self.log)
        self.assertFalse(dr._cycle_lock.locked())


class LoopTests(Base):
    async def test_startup_delay_then_interval_after_each_cycle_completes(self):
        events = []

        async def sleep(seconds):
            events.append(("sleep", seconds))
            if len([e for e in events if e[0] == "sleep"]) > 3:
                raise asyncio.CancelledError

        async def cycle():
            events.append(("cycle_start",))
            await asyncio.sleep(0)
            events.append(("cycle_end",))
            return "succeeded"

        with self.assertRaises(asyncio.CancelledError):
            await dr.defillama_refresh_loop(startup_delay=60, interval=21_600, cycle=cycle, sleep=sleep)
        self.assertEqual(events, [("sleep", 60), ("cycle_start",), ("cycle_end",), ("sleep", 21_600),
                                  ("cycle_start",), ("cycle_end",), ("sleep", 21_600), ("cycle_start",), ("cycle_end",), ("sleep", 21_600)])
        s = dr.refresh_status()
        self.assertEqual((s["state"], s["next_run_at"]), ("stopped", None))

    async def test_failed_cycle_is_retried_next_interval(self):
        outcomes, sleeps = iter(["failed", "succeeded"]), []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise asyncio.CancelledError

        async def cycle():
            return next(outcomes)

        with self.assertRaises(asyncio.CancelledError):
            await dr.defillama_refresh_loop(startup_delay=1, interval=5, cycle=cycle, sleep=sleep)
        self.assertEqual(sleeps, [1, 5, 5])

    async def test_next_run_is_reported_while_waiting(self):
        seen = []

        async def sleep(seconds):
            seen.append(dr.refresh_status()["next_run_at"])
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await dr.defillama_refresh_loop(startup_delay=60, interval=5, cycle=None, sleep=sleep)
        self.assertIsNotNone(seen[0])


class RestartStatusTests(unittest.TestCase):
    def test_fresh_status_never_claims_success(self):
        dr.reset_status()
        s = dr.refresh_status()
        self.assertIsNone(s["last_success_at"])
        self.assertIsNone(s["last_outcome"])
        self.assertFalse(s["running"])
        self.assertIn("unknown after an API restart", s["note"])


class StatusApiTests(unittest.TestCase):
    def test_availability_is_read_from_stored_days_and_separate_from_refresh(self):
        rows = [{"metric": "tvl.usd.daily", "slug": "raydium", "latest_day": date(2026, 11, 13)},
                {"metric": "tvl.usd.daily", "slug": "drift", "latest_day": date(2026, 11, 12)},
                {"metric": "tokens.qty.daily", "slug": "jito", "latest_day": date(2026, 11, 13)}]

        class Conn:
            async def fetch(self, query, *args):
                assert query == history.LATEST_BY_METRIC_QUERY
                return rows

        async def pool():
            return FakePool(Conn())

        dr.reset_status()
        app = FastAPI()
        app.include_router(history.router, prefix="/api/v1/history")
        with patch.object(history, "get_pool", pool):
            body = TestClient(app).get("/api/v1/history/refresh-status").json()
        self.assertEqual(body["availability"]["available_through"], "2026-11-13")
        self.assertEqual(body["availability"]["metrics"]["tvl.usd.daily"]["lagging_protocols"], ["drift"])
        self.assertIsNone(body["refresh"]["last_success_at"])

    def test_availability_failure_is_reported_not_raised(self):
        async def pool():
            raise OSError("db down")

        app = FastAPI()
        app.include_router(history.router, prefix="/api/v1/history")
        with patch.object(history, "get_pool", pool):
            res = TestClient(app).get("/api/v1/history/refresh-status")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["availability"], {"ok": False, "reason": "unavailable"})


if __name__ == "__main__":
    unittest.main()
