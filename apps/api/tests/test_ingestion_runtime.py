"""End-to-end ingestion regressions: DefiLlama -> get_protocol_metrics ->
_ingest_protocols -> snapshot INSERT -> rule-based scoring, with HTTP faked at
the transport layer so the real client code (and its error handling) runs."""

import io
import logging
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import httpx

from app.log_redaction import RedactingFilter, redact
from app.tasks import ingest
from tests.test_ingestion_reliability import FakeIngestConnection, FakeIngestPool

SECRET_KEY = "TEST-SECRET-HELIUS-KEY-0123456789"
SECRET_BOT = "123456789:TEST-SECRET-BOT-TOKEN_abcdef"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def fake_http(tvl_status: int = 200, tvl_value: float = 1_234_567.0, helius_status: int = 401):
    """Patch httpx.AsyncClient so every client the app creates uses a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.llama.fi":
            if tvl_status != 200:
                return httpx.Response(tvl_status, request=request)
            return httpx.Response(200, json={"tvl": [{"totalLiquidityUSD": tvl_value}]}, request=request)
        if "helius" in request.url.host:
            return httpx.Response(helius_status, json={"error": "unauthorized"}, request=request)
        raise AssertionError(f"unexpected host {request.url.host}")

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return patch("httpx.AsyncClient", side_effect=client_factory)


def helius_with_secret():
    return (
        patch("app.solana.client.settings.HELIUS_API_KEY", SECRET_KEY),
        patch("app.solana.client.settings.HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key="),
        patch("app.solana.client._client", None),
    )


class CaptureAllLogs:
    """Capture every record at DEBUG through a handler carrying the production
    redaction filter, with httpx request logging re-enabled for the test."""

    def __enter__(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        self.handler.addFilter(RedactingFilter())
        self.root = logging.getLogger()
        self.httpx_logger = logging.getLogger("httpx")
        self.saved = (self.root.level, self.httpx_logger.level)
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.DEBUG)
        self.httpx_logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.saved[0])
        self.httpx_logger.setLevel(self.saved[1])
        return False

    @property
    def text(self) -> str:
        return self.stream.getvalue()


def one_protocol():
    return patch("app.tasks.ingest.LAUNCH_PROTOCOLS", {"orca": {}})


class RuntimeIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_tvl_and_helius_401_persists_snapshot_with_null_tx(self) -> None:
        conn = FakeIngestConnection(history=[{"tvlUsd": 1_234_567.0, "txCount24h": None}])
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(), key_patch, url_patch, client_patch, patch(
            "app.tasks.ingest.LAUNCH_PROTOCOLS", {"orca": {}}
        ), CaptureAllLogs() as logs:
            written = await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertEqual(written, 1)
        query, args = conn.executed[0]
        self.assertIn('INSERT INTO protocol_snapshots', query)
        self.assertIn('(id, "protocolId", "tvlUsd", "txCount24h", "snapshotAt")', query)
        uuid.UUID(args[0])
        self.assertEqual(args[1], "protocol-id")
        self.assertEqual(args[2], 1_234_567.0)
        self.assertIsNone(args[3])
        self.assertIn("HTTP 401", logs.text)
        self.assertIn("tx24h=unavailable", logs.text)

    async def test_missing_tvl_skips_snapshot_and_never_calls_helius(self) -> None:
        conn = FakeIngestConnection(history=[])
        rpc = AsyncMock()
        with fake_http(tvl_status=404), one_protocol(), patch(
            "app.solana.protocols.get_solana_client", return_value=type("C", (), {"count_signatures_since": rpc})()
        ):
            written = await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertEqual(written, 0)
        self.assertEqual(conn.executed, [])
        rpc.assert_not_awaited()

    async def test_invalid_tvl_value_skips_snapshot(self) -> None:
        conn = FakeIngestConnection(history=[])
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(tvl_value=0.0), key_patch, url_patch, client_patch, one_protocol():
            written = await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertEqual(written, 0)
        self.assertEqual(conn.executed, [])

    async def test_scoring_failure_does_not_discard_saved_snapshot(self) -> None:
        conn = FakeIngestConnection(
            history=[{"tvlUsd": 1_234_567.0, "txCount24h": None}, {"tvlUsd": 1_200_000.0, "txCount24h": 7}]
        )
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(), key_patch, url_patch, client_patch, one_protocol(), patch(
            "app.tasks.ingest.score_from_snapshots", side_effect=RuntimeError("scorer exploded")
        ), CaptureAllLogs() as logs:
            written = await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertEqual(written, 1)
        self.assertEqual(len(conn.executed), 1)
        self.assertIn("INSERT INTO protocol_snapshots", conn.executed[0][0])
        self.assertIn("Snapshot saved for orca, but risk scoring failed", logs.text)
        self.assertNotIn("Ingestion failed for orca", logs.text)

    async def test_snapshot_insert_failure_is_not_counted(self) -> None:
        conn = FakeIngestConnection(history=[])
        conn.execute = AsyncMock(side_effect=RuntimeError("insert rejected"))
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(), key_patch, url_patch, client_patch, one_protocol():
            self.assertEqual(await ingest._ingest_protocols(FakeIngestPool(conn)), 0)


class SecretLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_secret_never_reaches_logs_even_with_httpx_debug(self) -> None:
        conn = FakeIngestConnection(history=[{"tvlUsd": 1_234_567.0, "txCount24h": None}])
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(), key_patch, url_patch, client_patch, one_protocol(), CaptureAllLogs() as logs:
            await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertIn("helius-rpc.com", logs.text)  # httpx request line was emitted...
        self.assertIn("api-key=<redacted>", logs.text)  # ...but redacted
        self.assertNotIn(SECRET_KEY, logs.text)

    async def test_application_warning_contains_no_url_even_without_filter(self) -> None:
        key_patch, url_patch, client_patch = helius_with_secret()
        with fake_http(), key_patch, url_patch, client_patch, self.assertLogs(
            "app.solana.protocols", level="WARNING"
        ) as captured:
            from app.solana.protocols import get_protocol_metrics

            await get_protocol_metrics("orca")

        joined = "\n".join(captured.output)
        self.assertNotIn(SECRET_KEY, joined)
        self.assertNotIn("api-key", joined)

    def test_traceback_and_args_are_redacted(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactingFilter())
        log = logging.getLogger("test.redaction")
        log.addHandler(handler)
        log.propagate = False
        try:
            try:
                raise RuntimeError(f"boom for url 'https://mainnet.helius-rpc.com/?api-key={SECRET_KEY}'")
            except RuntimeError:
                log.exception("failure calling %s", f"https://api.telegram.org/bot{SECRET_BOT}/sendMessage")
        finally:
            log.removeHandler(handler)
            log.propagate = True

        output = stream.getvalue()
        self.assertNotIn(SECRET_KEY, output)
        self.assertNotIn(SECRET_BOT, output)
        self.assertIn("api-key=<redacted>", output)
        self.assertIn("/bot<redacted>/sendMessage", output)
        self.assertIn("Traceback", output)

    def test_redact_leaves_ordinary_text_alone(self) -> None:
        text = "Snapshot written: orca TVL=$1,234,567 tx24h=unavailable https://api.llama.fi/protocol/orca"
        self.assertEqual(redact(text), text)


@unittest.skipUnless(
    os.environ.get("SOLANAGUARD_TEST_DATABASE_URL"),
    "set SOLANAGUARD_TEST_DATABASE_URL to a disposable database to run",
)
class NullInsertIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Opt-in: proves the real column accepts NULL. Runs inside a transaction
    that is always rolled back, so no row is persisted."""

    async def test_snapshot_insert_accepts_null_tx_count(self) -> None:
        import asyncpg
        from datetime import datetime

        conn = await asyncpg.connect(os.environ["SOLANAGUARD_TEST_DATABASE_URL"])
        try:
            transaction = conn.transaction()
            await transaction.start()
            try:
                protocol_id = await conn.fetchval("SELECT id FROM protocols LIMIT 1")
                self.assertIsNotNone(protocol_id, "test database needs at least one protocol row")
                snapshot_id = str(uuid.uuid4())
                await conn.execute(
                    'INSERT INTO protocol_snapshots (id, "protocolId", "tvlUsd", "txCount24h", "snapshotAt") '
                    "VALUES ($1, $2, $3, $4, $5)",
                    snapshot_id, protocol_id, 1.0, None, datetime(2000, 1, 1),
                )
                stored = await conn.fetchval(
                    'SELECT "txCount24h" FROM protocol_snapshots WHERE id = $1', snapshot_id
                )
                self.assertIsNone(stored)
            finally:
                await transaction.rollback()
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
