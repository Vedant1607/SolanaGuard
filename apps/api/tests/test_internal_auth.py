"""Internal-secret protection, Telegram webhook hardening, production settings
and the hosted history-refresh script. All secrets below are test values."""

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from fastapi.testclient import TestClient

from app import auth
from app import main
from app.config import ConfigurationError, Settings, production_config_problems, settings, validate_settings
from app.routers import protocols as protocols_router
from app.routers import telegram as telegram_router
from scripts import refresh_defillama_history as cron

SECRET = "t" * 40
TG_SECRET = "telegram-test-secret_123"
INTERNAL = auth.INTERNAL_SECRET_HEADER
TELEGRAM = auth.TELEGRAM_SECRET_HEADER

client = TestClient(main.app)  # no `with`: the lifespan (background loops) is not started


def configured(**values):
    return patch.multiple(settings, **values)


class InternalEndpointTests(unittest.TestCase):
    ENDPOINTS = [
        ("/api/v1/protocols/ingest", {}),
        ("/api/v1/protocols/raydium/test-alert", {"to_email": "ops@example.test"}),
        ("/api/v1/telegram/test-message", {"chat_id": "42"}),
    ]

    def setUp(self):
        self.ingest = AsyncMock(return_value=7)
        self.email = AsyncMock(return_value=True)
        self.telegram = AsyncMock(return_value=True)
        for p in (patch.object(protocols_router, "ingest_all_protocols", self.ingest),
                  patch.object(protocols_router, "send_alert_email", self.email),
                  patch.object(telegram_router, "send_telegram_message", self.telegram)):
            p.start()
            self.addCleanup(p.stop)

    def assert_nothing_ran(self):
        self.ingest.assert_not_called()
        self.email.assert_not_called()
        self.telegram.assert_not_called()

    def test_missing_server_secret_fails_closed(self):
        with configured(INTERNAL_API_SECRET=""):
            for path, params in self.ENDPOINTS:
                for headers in ({}, {INTERNAL: ""}, {INTERNAL: SECRET}):
                    r = client.post(path, params=params, headers=headers)
                    self.assertEqual(r.status_code, 503, (path, headers))
        self.assert_nothing_ran()

    def test_missing_empty_or_wrong_header_is_rejected(self):
        with configured(INTERNAL_API_SECRET=SECRET):
            for path, params in self.ENDPOINTS:
                for headers in ({}, {INTERNAL: ""}, {INTERNAL: "wrong"}, {INTERNAL: SECRET[:-1]}, {INTERNAL: SECRET + "x"},
                                {"Authorization": f"Bearer {SECRET}"}):
                    r = client.post(path, params=params, headers=headers)
                    self.assertEqual(r.status_code, 401, (path, headers))
                    self.assertNotIn(SECRET, r.text)
        self.assert_nothing_ran()

    def test_secret_in_query_string_is_not_accepted(self):
        with configured(INTERNAL_API_SECRET=SECRET):
            r = client.post("/api/v1/protocols/ingest", params={"secret": SECRET, "X-Internal-Api-Secret": SECRET})
        self.assertEqual(r.status_code, 401)
        self.ingest.assert_not_called()

    def test_correct_secret_runs_the_existing_actions(self):
        with configured(INTERNAL_API_SECRET=SECRET):
            r = client.post("/api/v1/protocols/ingest", headers={INTERNAL: SECRET})
            self.assertEqual((r.status_code, r.json()), (200, {"snapshots_written": 7}))
            r = client.post("/api/v1/protocols/raydium/test-alert", params={"to_email": "ops@example.test"}, headers={INTERNAL: SECRET})
            self.assertEqual((r.status_code, r.json()), (200, {"sent": True}))
            r = client.post("/api/v1/telegram/test-message", params={"chat_id": "42", "text": "hi"}, headers={INTERNAL: SECRET})
            self.assertEqual((r.status_code, r.json()), (200, {"sent": True}))
        self.ingest.assert_awaited_once()
        self.assertEqual(self.email.await_args.kwargs["to_email"], "ops@example.test")
        self.assertEqual(self.email.await_args.kwargs["risk_level"], "HIGH")
        self.telegram.assert_awaited_once_with("42", "hi")

    def test_authorized_requests_still_validate_parameters(self):
        with configured(INTERNAL_API_SECRET=SECRET):
            r = client.post("/api/v1/protocols/raydium/test-alert", headers={INTERNAL: SECRET})
        self.assertEqual(r.status_code, 422)
        self.email.assert_not_called()

    def test_comparison_is_constant_time(self):
        with patch.object(auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest) as spy, configured(INTERNAL_API_SECRET=SECRET):
            client.post("/api/v1/protocols/ingest", headers={INTERNAL: "nope"})
        spy.assert_called_once()
        self.assertFalse(auth.secret_matches(None, SECRET))
        self.assertFalse(auth.secret_matches("", ""))
        self.assertFalse(auth.secret_matches("x", ""))
        self.assertTrue(auth.secret_matches("ünïcode", "ünïcode"))

    def test_read_endpoints_stay_public(self):
        with configured(INTERNAL_API_SECRET=SECRET):
            self.assertEqual(client.get("/health").status_code, 200)


class FakeConn:
    def __init__(self, result="UPDATE 1"):
        self.result, self.calls = result, []

    async def execute(self, *args):
        self.calls.append(args)
        return self.result


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return Ctx()


def make(**values):
    return Settings(DATABASE_URL="postgresql://x@localhost/x", _env_file=None, **values)


PROD_OK = dict(ENVIRONMENT="production", ALLOWED_ORIGINS=["https://app.example.test"], INTERNAL_API_SECRET=SECRET)


class ProductionSettingsTests(unittest.TestCase):
    def test_safe_defaults(self):
        s = make()
        self.assertEqual(s.ENVIRONMENT, "development")
        self.assertFalse(s.DEBUG)
        self.assertEqual(s.ALLOWED_ORIGINS, ["http://localhost:3000"])
        self.assertEqual(s.INTERNAL_API_SECRET, "")
        self.assertFalse(s.DEFILLAMA_HISTORY_REFRESH_ENABLED)
        self.assertEqual(production_config_problems(s), [])

    def test_development_keeps_local_behaviour(self):
        s = make(DEBUG=True, DEFILLAMA_HISTORY_REFRESH_ENABLED=True)
        self.assertIs(validate_settings(s), s)

    def test_valid_production_configuration(self):
        s = make(**PROD_OK)
        self.assertIs(validate_settings(s), s)
        self.assertEqual(production_config_problems(make(**{**PROD_OK, "INTERNAL_API_SECRET": ""})), [])  # endpoints then fail closed

    def test_production_requires_explicit_https_origins(self):
        base = {k: v for k, v in PROD_OK.items() if k != "ALLOWED_ORIGINS"}
        self.assertIn("ALLOWED_ORIGINS must be set explicitly", production_config_problems(make(**base)))
        self.assertIn("ALLOWED_ORIGINS must be set explicitly", production_config_problems(make(**base, ALLOWED_ORIGINS=[])))
        for bad in ("*", "http://app.example.test", "https://localhost:3000", "https://127.0.0.1", "https://app.example.test/",
                    "https://app.example.test/path", "app.example.test", "https://"):
            problems = production_config_problems(make(**base, ALLOWED_ORIGINS=[bad]))
            self.assertTrue(any("not an https origin" in p for p in problems), bad)

    def test_production_rejects_debug_short_secret_and_local_scheduler(self):
        problems = production_config_problems(make(**{**PROD_OK, "DEBUG": True, "INTERNAL_API_SECRET": "short",
                                                      "DEFILLAMA_HISTORY_REFRESH_ENABLED": True}))
        self.assertEqual(len(problems), 3)
        with self.assertRaises(ConfigurationError) as ctx:
            validate_settings(make(**{**PROD_OK, "INTERNAL_API_SECRET": "s3cr3t-value"}))
        self.assertNotIn("s3cr3t-value", str(ctx.exception))  # names settings, never values

    def test_environment_is_read_from_the_environment(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "ALLOWED_ORIGINS": '["https://app.example.test"]', "DEBUG": "false"}):
            s = Settings(_env_file=None)
        self.assertEqual((s.ENVIRONMENT, s.ALLOWED_ORIGINS), ("production", ["https://app.example.test"]))
        self.assertEqual(production_config_problems(s), [])


class CorsTests(unittest.TestCase):
    def test_cors_allows_get_only_without_credentials(self):
        origin = settings.ALLOWED_ORIGINS[0]
        r = client.options("/api/v1/protocols/ingest", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status_code, 400)
        r = client.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("access-control-allow-credentials", r.headers)
        r = client.options("/health", headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "GET"})
        self.assertEqual(r.status_code, 400)


class TargetConn:
    def __init__(self, database=None, error=None):
        self.database, self.error = database, error

    async def fetchval(self, query):
        if self.error:
            raise self.error
        return self.database


class CronScriptTests(unittest.IsolatedAsyncioTestCase):
    def jobs(self, outcome, statuses=()):
        calls = []

        async def run(pool):
            calls.append(pool)
            return outcome, list(statuses)
        return run, calls

    async def test_target_must_be_configured_and_match(self):
        for expected, conn, reason in (("", TargetConn("neondb"), "expected_database_not_configured"),
                                       ("neondb", TargetConn("other"), "database_mismatch"),
                                       ("neondb", TargetConn(error=OSError("boom")), "target_check_failed:OSError")):
            run, calls = self.jobs("succeeded")
            code, summary = await cron.refresh(expected, pool=FakePool(conn), jobs=run)
            self.assertEqual(code, cron.EXIT_TARGET, reason)
            self.assertEqual(summary["target"]["reason"], reason)
            self.assertEqual(calls, [], "no backfill may run without a verified target")

    async def test_no_connection_is_opened_without_a_target_and_connect_errors_fail_cleanly(self):
        connect = AsyncMock(side_effect=ConnectionRefusedError("Connect call failed"))
        run, calls = self.jobs("succeeded")
        with patch.object(cron, "get_pool", connect), patch.object(cron, "close_pool", AsyncMock()):
            code, summary = await cron.refresh("", jobs=run)
            connect.assert_not_called()
            self.assertEqual((code, summary["target"]["reason"]), (cron.EXIT_TARGET, "expected_database_not_configured"))
            code, summary = await cron.refresh("neondb", jobs=run)
        self.assertEqual((code, summary["target"]["reason"]), (cron.EXIT_TARGET, "target_check_failed:ConnectionRefusedError"))
        self.assertEqual(calls, [])

    async def test_outcomes_map_to_exit_codes(self):
        status = SimpleNamespace(job="tvl_history", outcome="succeeded")
        with patch.object(cron, "asdict", lambda j: vars(j)):
            for outcome, expected in (("succeeded", cron.EXIT_OK), ("skipped_locked", cron.EXIT_OK),
                                      ("partial", cron.EXIT_JOB_FAILED), ("failed", cron.EXIT_JOB_FAILED)):
                run, calls = self.jobs(outcome, [status])
                code, summary = await cron.refresh("neondb", pool=FakePool(TargetConn("neondb")), jobs=run)
                self.assertEqual((code, summary["outcome"]), (expected, outcome))
                self.assertEqual(len(calls), 1)

    def test_main_reads_the_expected_database_and_prints_a_summary(self):
        async def fake_refresh(expected):
            return cron.EXIT_TARGET, {"target": {"expected_database": expected}, "outcome": "skipped_target", "jobs": []}
        out = io.StringIO()
        with patch.dict(os.environ, {cron.EXPECTED_DATABASE_ENV: "neondb"}), patch.object(cron, "refresh", fake_refresh), redirect_stdout(out):
            code = cron.main([])
        self.assertEqual(code, cron.EXIT_TARGET)
        self.assertEqual(json.loads(out.getvalue())["target"]["expected_database"], "neondb")

    async def test_cron_uses_the_shared_locked_runner(self):
        from app.tasks import defillama_refresh
        self.assertIs(cron.run_locked_jobs, defillama_refresh.run_locked_jobs)


if __name__ == "__main__":
    unittest.main()
