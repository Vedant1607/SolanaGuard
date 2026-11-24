"""Secure Telegram account linking: webhook routing (fakes, always run) and token
consumption against real PostgreSQL (opt-in).

The PostgreSQL tests run only when SOLANAGUARD_TEST_DATABASE_URL points at a
DISPOSABLE database. They never touch existing tables: each test class creates
its own schema, builds a minimal ``users`` table, applies the real migration
``20261120120000_telegram_link_tokens`` inside it, and drops the schema after.
All identifiers and tokens below are fake.
"""

import asyncio
import io
import logging
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

import asyncpg
from fastapi.testclient import TestClient

from app import auth, main
from app import telegram_linking as linking
from app.config import settings
from app.routers import telegram as telegram_router
from app.telegram_linking import LinkResult

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "packages/database/prisma/migrations/20261120120000_telegram_link_tokens/migration.sql"
TG_SECRET = "telegram-test-secret_123"
HEADERS = {auth.TELEGRAM_SECRET_HEADER: TG_SECRET}
# Shared with apps/web/src/lib/telegram-link-core.test.mjs.
VECTOR_TOKEN = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
VECTOR_HASH = "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0"

client = TestClient(main.app)  # lifespan not started


def update(text, chat_type="private", chat_id=555):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}


class TokenFormatTests(unittest.TestCase):
    def test_hash_matches_the_web_app(self):
        self.assertEqual(linking.hash_token(VECTOR_TOKEN), VECTOR_HASH)

    def test_only_43_char_base64url_tokens_are_accepted(self):
        self.assertTrue(linking.is_well_formed(VECTOR_TOKEN))
        for bad in ("", "user-123", "clxyz0000000000000000000", VECTOR_TOKEN + "A", VECTOR_TOKEN[:-1] + "=",
                    VECTOR_TOKEN[:-1] + "/", "../" + VECTOR_TOKEN[3:], VECTOR_TOKEN[:-1] + "é"):
            self.assertFalse(linking.is_well_formed(bad), bad)

    def test_malformed_tokens_never_reach_the_database(self):
        pool = AsyncMock()
        result = asyncio.run(linking.consume_link_token(pool, "user-123", "555"))
        self.assertIs(result, LinkResult.INVALID)
        pool.acquire.assert_not_called()


class WebhookRoutingTests(unittest.TestCase):
    """The webhook layer: secret header, chat-type filter, replies, logging."""

    def setUp(self):
        self.consume = AsyncMock(return_value=LinkResult.LINKED)
        self.sent = AsyncMock(return_value=True)
        self.pool = object()
        for p in (patch.object(telegram_router, "consume_link_token", self.consume),
                  patch.object(telegram_router, "send_telegram_message", self.sent),
                  patch.object(telegram_router, "get_pool", AsyncMock(return_value=self.pool)),
                  patch.multiple(settings, TELEGRAM_WEBHOOK_SECRET=TG_SECRET)):
            p.start()
            self.addCleanup(p.stop)

    def post(self, body, headers=HEADERS):
        return client.post("/api/v1/telegram/webhook", json=body, headers=headers)

    def test_secret_protections_are_intact(self):
        for headers in ({}, {auth.TELEGRAM_SECRET_HEADER: "wrong"}):
            self.assertEqual(self.post(update(f"/start {VECTOR_TOKEN}"), headers).status_code, 401)
        with patch.multiple(settings, TELEGRAM_WEBHOOK_SECRET=""):
            self.assertEqual(self.post(update(f"/start {VECTOR_TOKEN}")).status_code, 503)
        self.consume.assert_not_called()
        self.sent.assert_not_called()

    def test_valid_private_start_consumes_the_token_for_that_chat(self):
        r = self.post(update(f"/start {VECTOR_TOKEN}"))
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.consume.assert_awaited_once_with(self.pool, VECTOR_TOKEN, "555")
        self.assertEqual(self.sent.await_args.args, (555, telegram_router.MSG_LINKED))

    def test_bot_mention_form_is_accepted(self):
        self.post(update(f"/start@SolanaGuardBot {VECTOR_TOKEN}"))
        self.consume.assert_awaited_once_with(self.pool, VECTOR_TOKEN, "555")

    def test_group_supergroup_and_channel_chats_are_never_linked(self):
        for chat_type in ("group", "supergroup", "channel", None):
            self.post(update(f"/start {VECTOR_TOKEN}", chat_type=chat_type))
        self.post({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": f"/start {VECTOR_TOKEN}"}})
        self.consume.assert_not_called()
        self.sent.assert_not_called()

    def test_every_failure_gets_the_same_generic_reply(self):
        self.consume.return_value = LinkResult.INVALID
        self.post(update("/start user-123"))  # legacy raw-ID links are just invalid tokens now
        self.assertEqual(self.sent.await_args.args[1], telegram_router.MSG_INVALID)
        self.assertNotIn("account", telegram_router.MSG_INVALID.lower())  # says nothing about any user

    def test_other_results_and_messages(self):
        for result, reply in ((LinkResult.ALREADY_LINKED, telegram_router.MSG_ALREADY_LINKED),
                              (LinkResult.CHAT_IN_USE, telegram_router.MSG_CHAT_IN_USE)):
            self.consume.return_value = result
            self.post(update(f"/start {VECTOR_TOKEN}"))
            self.assertEqual(self.sent.await_args.args[1], reply)
        self.consume.reset_mock()
        self.post(update("/start"))
        self.assertEqual(self.sent.await_args.args[1], telegram_router.MSG_HELP)
        for body in (update("hello"), update("/startx abc"), {"edited_message": {}}, {"message": "x"},
                     {"message": {"chat": {"id": "555", "type": "private"}, "text": "/start abc"}}, []):
            self.assertEqual(self.post(body).status_code, 200)
        self.consume.assert_not_called()

    def test_token_is_never_logged(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            self.post(update(f"/start {VECTOR_TOKEN}"))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertIn("linked", stream.getvalue())
        self.assertNotIn(VECTOR_TOKEN, stream.getvalue())
        self.assertNotIn(VECTOR_HASH, stream.getvalue())


TEST_DB = os.environ.get("SOLANAGUARD_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set SOLANAGUARD_TEST_DATABASE_URL to a DISPOSABLE database to run")
class _PgBase(unittest.IsolatedAsyncioTestCase):
    """Own schema per test: minimal users table + the real migration; dropped afterwards."""

    async def asyncSetUp(self):
        self.schema = f"tg_link_test_{uuid.uuid4().hex[:12]}"
        self.admin = await asyncpg.connect(TEST_DB)
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        # Non-UTC session time zone: expiry checks must not depend on it.
        self.pool = await asyncpg.create_pool(
            TEST_DB, min_size=1, max_size=12,
            server_settings={"search_path": self.schema, "TimeZone": "Asia/Kolkata"})
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, "clerkId" TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
                    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP, "telegramChatId" TEXT)""")
            await conn.execute("""INSERT INTO users (id, "clerkId", email) VALUES
                ('u1', 'user_clerk_a', 'a@example.test'), ('u2', 'user_clerk_b', 'b@example.test')""")
            await conn.execute(MIGRATION.read_text())

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def issue(self, user_id, token, *, minutes=10, consumed=False, revoked=False):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO telegram_link_tokens (id, "userId", "tokenHash", "createdAt", "expiresAt", "consumedAt", "revokedAt")
                   VALUES ($1, $2, $3, (now() AT TIME ZONE 'UTC') + make_interval(mins => $4) - interval '10 minutes',
                           (now() AT TIME ZONE 'UTC') + make_interval(mins => $4),
                           CASE WHEN $5 THEN now() AT TIME ZONE 'UTC' END, CASE WHEN $6 THEN now() AT TIME ZONE 'UTC' END)""",
                uuid.uuid4().hex, user_id, linking.hash_token(token), minutes, consumed, revoked)

    async def chat_of(self, user_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT "telegramChatId" FROM users WHERE id = $1', user_id)

    async def consumed(self, token):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('SELECT "consumedAt" IS NOT NULL FROM telegram_link_tokens WHERE "tokenHash" = $1',
                                       linking.hash_token(token))

    @staticmethod
    def token(n):
        return (f"{n:02d}" * 22)[:43]

    async def usable(self, user_id):
        """Hashes the webhook would accept for this user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT "tokenHash" FROM telegram_link_tokens WHERE "userId" = $1 AND "consumedAt" IS NULL
                   AND "revokedAt" IS NULL AND "expiresAt" > (now() AT TIME ZONE 'UTC')""", user_id)
        return sorted(r["tokenHash"] for r in rows)

    async def lock_waiters(self):
        return await self.admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")

    async def wait_for_lock_waiters(self, n):
        for _ in range(300):
            if await self.lock_waiters() >= n:
                return
            await asyncio.sleep(0.01)
        self.fail(f"expected {n} transaction(s) waiting on a lock")


class PostgresLinkingTests(_PgBase):
    async def test_valid_token_links_the_right_user_and_is_consumed(self):
        await self.issue("u2", self.token(1))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(1), "555"), LinkResult.LINKED)
        self.assertEqual((await self.chat_of("u2"), await self.chat_of("u1")), ("555", None))
        self.assertTrue(await self.consumed(self.token(1)))

    async def test_unknown_expired_and_revoked_tokens_are_rejected(self):
        await self.issue("u1", self.token(2), minutes=-1)  # expired a minute ago
        await self.issue("u1", self.token(3), revoked=True)
        for token in (self.token(2), self.token(3), self.token(4)):
            self.assertIs(await linking.consume_link_token(self.pool, token, "555"), LinkResult.INVALID)
        self.assertIsNone(await self.chat_of("u1"))

    async def test_consumed_token_cannot_be_reused_by_another_chat(self):
        await self.issue("u1", self.token(5))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(5), "555"), LinkResult.LINKED)
        self.assertIs(await linking.consume_link_token(self.pool, self.token(5), "777"), LinkResult.INVALID)
        self.assertEqual(await self.chat_of("u1"), "555")

    async def test_telegram_redelivery_is_idempotent(self):
        await self.issue("u1", self.token(6))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(6), "555"), LinkResult.LINKED)
        self.assertIs(await linking.consume_link_token(self.pool, self.token(6), "555"), LinkResult.ALREADY_LINKED)
        self.assertEqual(await self.chat_of("u1"), "555")

    async def test_concurrent_deliveries_consume_a_token_exactly_once(self):
        await self.issue("u1", self.token(7))
        chats = [str(1000 + i) for i in range(10)]
        results = await asyncio.gather(*(linking.consume_link_token(self.pool, self.token(7), c) for c in chats))
        self.assertEqual(results.count(LinkResult.LINKED), 1, results)
        self.assertEqual(results.count(LinkResult.INVALID), 9, results)
        self.assertEqual(await self.chat_of("u1"), chats[results.index(LinkResult.LINKED)])

    async def test_a_held_row_lock_serialises_two_consumers(self):
        """Both consumers start while the token row is locked; exactly one wins after release."""
        await self.issue("u1", self.token(8))
        async with self.pool.acquire() as blocker:
            tx = blocker.transaction()
            await tx.start()
            await blocker.execute('SELECT 1 FROM telegram_link_tokens WHERE "tokenHash" = $1 FOR UPDATE',
                                  linking.hash_token(self.token(8)))
            tasks = [asyncio.create_task(linking.consume_link_token(self.pool, self.token(8), c)) for c in ("1", "2")]
            await asyncio.sleep(0.3)
            self.assertFalse(any(t.done() for t in tasks))  # both are blocked on the row lock
            await tx.rollback()
            results = sorted(r.value for r in await asyncio.gather(*tasks))
        self.assertEqual(results, ["invalid", "linked"])

    async def test_failure_after_consumption_rolls_everything_back(self):
        await self.issue("u1", self.token(9))
        with patch.object(linking, "LINK_CHAT", 'UPDATE users SET "telegramChatId" = $1 WHERE id = $2 AND 1/0 = 1'):
            with self.assertRaises(asyncpg.DivisionByZeroError):
                await linking.consume_link_token(self.pool, self.token(9), "555")
        self.assertFalse(await self.consumed(self.token(9)))  # token consumption rolled back too
        self.assertIsNone(await self.chat_of("u1"))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(9), "555"), LinkResult.LINKED)

    async def test_a_chat_linked_to_another_account_is_never_reassigned(self):
        await self.issue("u1", self.token(10))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(10), "555"), LinkResult.LINKED)
        await self.issue("u2", self.token(11))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(11), "555"), LinkResult.CHAT_IN_USE)
        self.assertEqual((await self.chat_of("u1"), await self.chat_of("u2")), ("555", None))
        self.assertFalse(await self.consumed(self.token(11)))  # rolled back, nothing changed

    async def test_concurrent_claims_of_one_chat_by_two_accounts(self):
        await self.issue("u1", self.token(12))
        await self.issue("u2", self.token(13))
        results = await asyncio.gather(linking.consume_link_token(self.pool, self.token(12), "555"),
                                       linking.consume_link_token(self.pool, self.token(13), "555"))
        self.assertEqual(sorted(r.value for r in results), ["chat_in_use", "linked"])
        chats = [await self.chat_of("u1"), await self.chat_of("u2")]
        self.assertEqual(chats.count("555"), 1)

    async def test_user_can_move_their_own_alerts_to_a_new_chat(self):
        await self.issue("u1", self.token(14))
        await linking.consume_link_token(self.pool, self.token(14), "555")
        await self.issue("u1", self.token(15))
        self.assertIs(await linking.consume_link_token(self.pool, self.token(15), "556"), LinkResult.LINKED)
        self.assertEqual(await self.chat_of("u1"), "556")

    async def test_schema_rejects_plaintext_tokens_and_duplicate_chats(self):
        async with self.pool.acquire() as conn:
            with self.assertRaises(asyncpg.CheckViolationError):
                await conn.execute("""INSERT INTO telegram_link_tokens (id, "userId", "tokenHash", "expiresAt")
                                      VALUES ('x', 'u1', $1, (now() AT TIME ZONE 'UTC') + interval '10 minutes')""",
                                   self.token(16))
            await conn.execute("""UPDATE users SET "telegramChatId" = '900' WHERE id = 'u1'""")
            with self.assertRaises(asyncpg.UniqueViolationError):
                await conn.execute("""UPDATE users SET "telegramChatId" = '900' WHERE id = 'u2'""")
            await conn.execute("DELETE FROM users WHERE id = 'u2'")  # FK cascades to tokens
            self.assertEqual(await conn.fetchval('SELECT count(*) FROM telegram_link_tokens WHERE "userId" = $1', "u2"), 0)


# Issuance as the web app performs it (apps/web/src/lib/telegram-link-store.ts):
# the same three statements in the same order, inside one transaction.
WEB_LOCK_USER = "SELECT id FROM users WHERE id = $1 FOR UPDATE"
WEB_REVOKE = """UPDATE telegram_link_tokens SET "revokedAt" = (now() AT TIME ZONE 'UTC')
                WHERE "userId" = $1 AND "consumedAt" IS NULL AND "revokedAt" IS NULL"""
WEB_INSERT = """INSERT INTO telegram_link_tokens (id, "userId", "tokenHash", "createdAt", "expiresAt")
                VALUES ($1, $2, $3, (now() AT TIME ZONE 'UTC'), (now() AT TIME ZONE 'UTC') + interval '10 minutes')"""

# The consumption order before this fix: token row first, users row second.
OLD_CONSUME = """UPDATE telegram_link_tokens SET "consumedAt" = (now() AT TIME ZONE 'UTC')
                 WHERE "tokenHash" = $1 AND "consumedAt" IS NULL AND "revokedAt" IS NULL RETURNING "userId" """


class LockOrderTests(_PgBase):
    """Issuance vs consumption and cross-user claims under controlled contention."""

    async def web_issue(self, user_id, token):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchval(WEB_LOCK_USER, user_id)
            await conn.execute(WEB_REVOKE, user_id)
            await conn.execute(WEB_INSERT, uuid.uuid4().hex, user_id, linking.hash_token(token))

    async def test_control_previous_order_deadlocks_against_issuance(self):
        """Proves the inversion was real: token-then-user vs user-then-token."""
        await self.issue("u1", self.token(20))
        consumer, issuer = await asyncpg.connect(TEST_DB), await asyncpg.connect(TEST_DB)
        try:
            for c in (consumer, issuer):
                await c.execute(f'SET search_path TO "{self.schema}"')
                await c.execute("BEGIN")
            self.assertEqual(await consumer.fetchval(OLD_CONSUME, linking.hash_token(self.token(20))), "u1")  # holds token
            await issuer.fetchval(WEB_LOCK_USER, "u1")                                                      # holds users[u1]
            revoke = asyncio.create_task(issuer.execute(WEB_REVOKE, "u1"))                                  # waits for token
            await self.wait_for_lock_waiters(1)
            link = asyncio.create_task(consumer.execute(linking.LINK_CHAT, "555", "u1"))                   # waits for users[u1]
            results = await asyncio.wait_for(asyncio.gather(revoke, link, return_exceptions=True), 15)
            self.assertEqual(sum(isinstance(r, asyncpg.DeadlockDetectedError) for r in results), 1, results)
        finally:
            for c in (consumer, issuer):
                await c.close()  # rolls back whatever survived
        self.assertIsNone(await self.chat_of("u1"))

    async def test_issuance_holding_the_user_lock_no_longer_deadlocks(self):
        """The exact interleaving above with the current consumer: it waits on users[u1]
        WITHOUT holding the token, so issuance proceeds; the revoked token is then rejected."""
        await self.issue("u1", self.token(21))
        async with self.pool.acquire() as issuer:
            tx = issuer.transaction()
            await tx.start()
            await issuer.fetchval(WEB_LOCK_USER, "u1")
            consume = asyncio.create_task(linking.consume_link_token(self.pool, self.token(21), "555"))
            await self.wait_for_lock_waiters(1)
            await asyncio.wait_for(issuer.execute(WEB_REVOKE, "u1"), 3)  # would block (deadlock) with the old order
            await issuer.execute(WEB_INSERT, uuid.uuid4().hex, "u1", linking.hash_token(self.token(22)))
            self.assertFalse(consume.done())
            await tx.commit()
        self.assertIs(await asyncio.wait_for(consume, 5), LinkResult.INVALID)
        self.assertIsNone(await self.chat_of("u1"))
        self.assertEqual(await self.usable("u1"), [linking.hash_token(self.token(22))])

    async def test_consumption_queued_before_issuance_links_then_new_token_survives(self):
        await self.issue("u1", self.token(23))
        async with self.pool.acquire() as blocker:
            tx = blocker.transaction()
            await tx.start()
            await blocker.fetchval(WEB_LOCK_USER, "u1")
            consume = asyncio.create_task(linking.consume_link_token(self.pool, self.token(23), "555"))
            await self.wait_for_lock_waiters(1)
            issue = asyncio.create_task(self.web_issue("u1", self.token(24)))
            await self.wait_for_lock_waiters(2)
            await tx.rollback()
        result, _ = await asyncio.wait_for(asyncio.gather(consume, issue), 10)
        self.assertIs(result, LinkResult.LINKED)
        self.assertEqual(await self.chat_of("u1"), "555")
        self.assertTrue(await self.consumed(self.token(23)))
        self.assertEqual(await self.usable("u1"), [linking.hash_token(self.token(24))])

    async def test_two_users_claiming_each_others_chats(self):
        """u1 owns 111 and u2 owns 222; each presents a valid token in the other's chat."""
        async with self.pool.acquire() as conn:
            await conn.execute("""UPDATE users SET "telegramChatId" = CASE id WHEN 'u1' THEN '111' ELSE '222' END""")
        await self.issue("u1", self.token(25))
        await self.issue("u2", self.token(26))
        async with self.pool.acquire() as blocker:  # make both start together, then release
            tx = blocker.transaction()
            await tx.start()
            await blocker.fetch("SELECT id FROM users WHERE id IN ('u1','u2') ORDER BY id FOR UPDATE")
            tasks = [asyncio.create_task(linking.consume_link_token(self.pool, self.token(25), "222")),
                     asyncio.create_task(linking.consume_link_token(self.pool, self.token(26), "111"))]
            await self.wait_for_lock_waiters(2)
            await tx.rollback()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertEqual(results, [LinkResult.CHAT_IN_USE, LinkResult.CHAT_IN_USE])
        self.assertEqual((await self.chat_of("u1"), await self.chat_of("u2")), ("111", "222"))
        self.assertFalse(await self.consumed(self.token(25)))
        self.assertFalse(await self.consumed(self.token(26)))
        for _ in range(20):  # and unsynchronised
            results = await asyncio.wait_for(asyncio.gather(
                linking.consume_link_token(self.pool, self.token(25), "222"),
                linking.consume_link_token(self.pool, self.token(26), "111")), 10)
            self.assertEqual(results, [LinkResult.CHAT_IN_USE, LinkResult.CHAT_IN_USE])
        self.assertEqual((await self.chat_of("u1"), await self.chat_of("u2")), ("111", "222"))

    async def test_two_users_racing_for_one_free_chat_and_swapping(self):
        await self.issue("u1", self.token(27))
        await self.issue("u2", self.token(28))
        results = await asyncio.wait_for(asyncio.gather(
            linking.consume_link_token(self.pool, self.token(27), "333"),
            linking.consume_link_token(self.pool, self.token(28), "333")), 10)
        self.assertEqual(sorted(r.value for r in results), ["chat_in_use", "linked"])
        winner, loser = ("u1", "u2") if results[0] is LinkResult.LINKED else ("u2", "u1")
        self.assertEqual((await self.chat_of(winner), await self.chat_of(loser)), ("333", None))
        loser_token = self.token(28) if loser == "u2" else self.token(27)
        self.assertFalse(await self.consumed(loser_token))  # rolled back, still usable elsewhere
        self.assertIs(await linking.consume_link_token(self.pool, loser_token, "444"), LinkResult.LINKED)

    async def test_mixed_burst_of_issuance_and_consumption_stays_consistent(self):
        users = [f"b{i}" for i in range(6)]
        async with self.pool.acquire() as conn:
            await conn.executemany("""INSERT INTO users (id, "clerkId", email) VALUES ($1, $1, $1 || '@example.test')""",
                                   [(u,) for u in users])
        rng = __import__("random").Random(1234)
        issued = {u: [] for u in users}
        counter = iter(range(10_000))
        linked = []

        def new_token():
            return f"{next(counter):043d}"

        for u in users:
            t = new_token()
            await self.web_issue(u, t)
            issued[u].append(t)
        for _round in range(8):
            ops = []
            for _ in range(24):
                u = rng.choice(users)
                if rng.random() < 0.4:
                    t = new_token()
                    issued[u].append(t)
                    ops.append(("issue", u, t, self.web_issue(u, t)))
                else:
                    t, chat = rng.choice(issued[u]), rng.choice(["c1", "c2", "c3", "c4"])
                    ops.append(("consume", u, t, linking.consume_link_token(self.pool, t, chat)))
            results = await asyncio.wait_for(asyncio.gather(*(op[3] for op in ops), return_exceptions=True), 30)
            errors = [r for r in results if isinstance(r, BaseException)]
            self.assertEqual(errors, [])  # in particular no DeadlockDetectedError
            linked += [op[2] for op, r in zip(ops, results) if r is LinkResult.LINKED]
            for u in users:  # invariants after every round
                self.assertLessEqual(len(await self.usable(u)), 1, u)
            async with self.pool.acquire() as conn:
                consumed = await conn.fetchval('SELECT count(*) FROM telegram_link_tokens WHERE "consumedAt" IS NOT NULL')
                dupes = await conn.fetchval('SELECT count(*) FROM (SELECT 1 FROM users WHERE "telegramChatId" IS NOT NULL '
                                            'GROUP BY "telegramChatId" HAVING count(*) > 1) d')
                unlinked = await conn.fetchval('SELECT count(*) FROM telegram_link_tokens t JOIN users u ON u.id = t."userId" '
                                               'WHERE t."consumedAt" IS NOT NULL AND u."telegramChatId" IS NULL')
            self.assertEqual(consumed, len(linked))  # every consumption is exactly one LINKED result
            self.assertEqual(len(set(linked)), len(linked))  # no token consumed twice
            self.assertEqual((dupes, unlinked), (0, 0))


@unittest.skipUnless(TEST_DB, "set SOLANAGUARD_TEST_DATABASE_URL to a DISPOSABLE database to run")
class MigrationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_refuses_existing_duplicate_chat_ids_and_changes_nothing(self):
        schema = f"tg_guard_test_{uuid.uuid4().hex[:12]}"
        conn = await asyncpg.connect(TEST_DB, server_settings={"search_path": schema})
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute('CREATE TABLE users (id TEXT PRIMARY KEY, "telegramChatId" TEXT)')
            await conn.execute("""INSERT INTO users VALUES ('u1', '42'), ('u2', '42')""")
            with self.assertRaisesRegex(asyncpg.RaiseError, "duplicate values"):
                await conn.execute(MIGRATION.read_text())
            self.assertIsNone(await conn.fetchval("SELECT to_regclass('telegram_link_tokens')"))
            self.assertEqual(await conn.fetchval("""SELECT count(*) FROM users WHERE "telegramChatId" = '42'"""), 2)
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.close()


if __name__ == "__main__":
    unittest.main()
