import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import httpx

from app.config import settings
from app.solana import protocols
from app.solana.client import SignatureCountUnavailable, SolanaClient
from app.tasks import ingest


def helius_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://rpc.invalid/")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


class FullPageRpc:
    """Fake RPC returning full 1,000-signature pages that never cross the boundary."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, method: str, params: list) -> list[dict]:
        self.calls += 1
        return [
            {"signature": f"sig-{self.calls}-{i}", "blockTime": 10_000}
            for i in range(1_000)
        ]


class SignaturePageLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_limit_bounds_rpc_calls_and_reports_unavailable(self) -> None:
        client = SolanaClient()
        rpc = FullPageRpc()
        client._rpc = rpc  # type: ignore[method-assign]

        with self.assertRaises(SignatureCountUnavailable):
            await client.count_signatures_since("program", 100, max_pages=3)

        self.assertEqual(rpc.calls, 3)

    async def test_default_page_limit_comes_from_settings(self) -> None:
        client = SolanaClient()
        rpc = FullPageRpc()
        client._rpc = rpc  # type: ignore[method-assign]

        with self.assertRaises(SignatureCountUnavailable):
            await client.count_signatures_since("program", 100)

        self.assertEqual(rpc.calls, settings.HELIUS_MAX_SIGNATURE_PAGES)

    async def test_exact_count_returned_when_boundary_reached_within_limit(self) -> None:
        client = SolanaClient()
        pages = [
            [{"signature": "a", "blockTime": 150}, {"signature": "b", "blockTime": 120}],
            [{"signature": "c", "blockTime": 110}, {"signature": "d", "blockTime": 90}],
        ]

        async def fake_rpc(method: str, params: list) -> list[dict]:
            return pages.pop(0)

        client._rpc = fake_rpc  # type: ignore[method-assign]

        self.assertEqual(await client.count_signatures_since("program", 100, max_pages=2), 3)

    async def test_invalid_page_limit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await SolanaClient().count_signatures_since("program", 100, max_pages=0)


class ProtocolMetricsAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    """txCount24h is nullable: an unavailable count is None, never a fabricated zero."""

    async def test_defillama_success_with_helius_401_keeps_tvl_with_null_count(self) -> None:
        client = SimpleNamespace(count_signatures_since=AsyncMock(side_effect=helius_401()))
        with (
            patch("app.solana.protocols.fetch_tvl", AsyncMock(return_value=1_000_000.0)),
            patch("app.solana.protocols.get_solana_client", return_value=client),
            self.assertLogs("app.solana.protocols", level="WARNING") as logs,
        ):
            metrics = await protocols.get_protocol_metrics("orca")

        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.tvl_usd, 1_000_000.0)
        self.assertIsNone(metrics.tx_count_24h)
        self.assertIn("HTTP 401", logs.output[0])
        self.assertNotIn("rpc.invalid", logs.output[0])  # request URL (holds the key) not logged

    async def test_page_limit_exhaustion_is_treated_as_unavailable(self) -> None:
        client = SimpleNamespace(
            count_signatures_since=AsyncMock(side_effect=SignatureCountUnavailable("too many"))
        )
        with (
            patch("app.solana.protocols.fetch_tvl", AsyncMock(return_value=1_000_000.0)),
            patch("app.solana.protocols.get_solana_client", return_value=client),
        ):
            metrics = await protocols.get_protocol_metrics("raydium")

        self.assertIsNotNone(metrics)
        self.assertIsNone(metrics.tx_count_24h)

    async def test_missing_tvl_still_skips_without_calling_helius(self) -> None:
        client = SimpleNamespace(count_signatures_since=AsyncMock(return_value=10))
        with (
            patch("app.solana.protocols.fetch_tvl", AsyncMock(return_value=None)),
            patch("app.solana.protocols.get_solana_client", return_value=client),
        ):
            self.assertIsNone(await protocols.get_protocol_metrics("orca"))

        client.count_signatures_since.assert_not_awaited()

    async def test_helius_success_keeps_exact_count(self) -> None:
        client = SimpleNamespace(count_signatures_since=AsyncMock(return_value=4_321))
        with (
            patch("app.solana.protocols.fetch_tvl", AsyncMock(return_value=1_000_000.0)),
            patch("app.solana.protocols.get_solana_client", return_value=client),
        ):
            metrics = await protocols.get_protocol_metrics("orca")

        self.assertEqual(metrics.tx_count_24h, 4_321)

    async def test_unavailable_metrics_write_no_snapshot(self) -> None:
        pool = SimpleNamespace(acquire=unittest.mock.Mock(side_effect=AssertionError("no DB use expected")))
        with patch("app.tasks.ingest.get_protocol_metrics", AsyncMock(return_value=None)):
            self.assertEqual(await ingest._ingest_protocols(pool), 0)
        pool.acquire.assert_not_called()


class FakeIngestConnection:
    """Records writes made by one ingestion cycle for a single protocol."""

    def __init__(self, history):
        self.history = history  # newest first, as returned by the LIMIT 2 query
        self.executed = []

    async def fetchval(self, query, *args):
        return "protocol-id"

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetch(self, query, *args):
        return self.history

    async def fetchrow(self, query, *args):
        return None


class FakeIngestPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        connection = self.connection

        class Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *exc):
                return None

        return Acquire()


class TvlOnlySnapshotIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_helius_failure_persists_snapshot_with_null_tx_and_tvl_only_score(self) -> None:
        metrics = protocols.ProtocolMetrics(slug="orca", tvl_usd=950_000.0, tx_count_24h=None)
        conn = FakeIngestConnection(
            history=[
                {"tvlUsd": 950_000.0, "txCount24h": None},
                {"tvlUsd": 1_000_000.0, "txCount24h": 5_000},
            ]
        )
        with (
            patch("app.tasks.ingest.LAUNCH_PROTOCOLS", {"orca": {}}),
            patch("app.tasks.ingest.get_protocol_metrics", AsyncMock(return_value=metrics)),
        ):
            written = await ingest._ingest_protocols(FakeIngestPool(conn))

        self.assertEqual(written, 1)
        snapshot_query, snapshot_args = conn.executed[0]
        self.assertIn("INSERT INTO protocol_snapshots", snapshot_query)
        self.assertEqual(snapshot_args[2], 950_000.0)  # tvlUsd
        self.assertIsNone(snapshot_args[3])  # txCount24h stored as NULL, not 0

        score_query, score_args = conn.executed[1]
        self.assertIn("INSERT INTO risk_scores", score_query)
        self.assertEqual(score_args[2], 30.0)  # 15 baseline + 15 for a 5% TVL drop; no tx term
        self.assertEqual(score_args[3], "LOW")
        self.assertIn("transaction count unavailable", score_args[5])


class FakeAlertConnection:
    def __init__(self, emails, chat_ids, telegram_error=None):
        self.emails = emails
        self.chat_ids = chat_ids
        self.telegram_error = telegram_error
        self.queries = []

    async def fetch(self, query, protocol_id):
        self.queries.append(query)
        if "telegramChatId" in query:
            if self.telegram_error:
                raise self.telegram_error
            return [{"telegramChatId": chat_id} for chat_id in self.chat_ids]
        return [{"email": email} for email in self.emails]


RESULT = SimpleNamespace(risk_level="HIGH", overall_score=70.0, explanation="TVL dropped 25.0%")


class EscalationAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_sent_when_no_telegram_recipients(self) -> None:
        conn = FakeAlertConnection(emails=["a@example.com", "b@example.com"], chat_ids=[])
        email = AsyncMock(return_value=True)
        telegram = AsyncMock(return_value=True)
        with (
            patch("app.tasks.ingest.send_alert_email", email),
            patch("app.tasks.ingest.send_telegram_message", telegram),
        ):
            await ingest._send_escalation_alerts(conn, "p1", "orca", RESULT)

        self.assertEqual(
            [call.kwargs["to_email"] for call in email.await_args_list],
            ["a@example.com", "b@example.com"],
        )
        telegram.assert_not_awaited()

    async def test_each_recipient_notified_once_per_channel(self) -> None:
        conn = FakeAlertConnection(emails=["a@example.com", "b@example.com"], chat_ids=["111", "222"])
        email = AsyncMock(return_value=True)
        telegram = AsyncMock(return_value=True)
        with (
            patch("app.tasks.ingest.send_alert_email", email),
            patch("app.tasks.ingest.send_telegram_message", telegram),
        ):
            await ingest._send_escalation_alerts(conn, "p1", "orca", RESULT)

        self.assertEqual(email.await_count, 2)
        self.assertEqual([call.args[0] for call in telegram.await_args_list], ["111", "222"])
        self.assertTrue(all("DISTINCT" in query for query in conn.queries))

    async def test_telegram_failure_does_not_block_email(self) -> None:
        conn = FakeAlertConnection(
            emails=["a@example.com"], chat_ids=[], telegram_error=RuntimeError("db down")
        )
        email = AsyncMock(return_value=True)
        with (
            patch("app.tasks.ingest.send_alert_email", email),
            patch("app.tasks.ingest.send_telegram_message", AsyncMock(return_value=False)),
        ):
            await ingest._send_escalation_alerts(conn, "p1", "orca", RESULT)

        email.assert_awaited_once()

    async def test_email_failure_does_not_block_telegram(self) -> None:
        conn = FakeAlertConnection(emails=["a@example.com"], chat_ids=["111"])
        telegram = AsyncMock(return_value=True)
        with (
            patch("app.tasks.ingest.send_alert_email", AsyncMock(side_effect=RuntimeError("resend down"))),
            patch("app.tasks.ingest.send_telegram_message", telegram),
        ):
            await ingest._send_escalation_alerts(conn, "p1", "orca", RESULT)

        telegram.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
