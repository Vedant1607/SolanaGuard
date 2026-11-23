"""Consume single-use Telegram link tokens (issued by the web app) and link chats.

The web app stores only ``sha256(token)`` as lowercase hex in
``telegram_link_tokens``; the plaintext token travels once, in the Telegram deep
link (``/start <token>``). Here the token is hashed and consumed inside one
transaction together with the chat link, so either both happen or neither does.

Lock order (the same in every writer, so no two of them can wait on each other
in a cycle; it must hold across any number of API / web instances):

    1. the token owner's ``users`` row      (SELECT ... FOR UPDATE)
    2. that user's ``telegram_link_tokens`` rows

Issuance (apps/web/src/lib/telegram-link-store.ts) locks ``users[U]`` and then
revokes U's unused tokens and inserts the new one. Consumption reads the
token's ``userId`` without locking (it never changes), locks ``users[U]``, and
only then consumes the token with a conditional ``UPDATE ... RETURNING``.
Under that lock, issuance and consumption for one user are serialised, and of
two deliveries of one token exactly one matches the predicate.

At most ONE ``users`` row is locked per transaction. The chat's current owner
is read without a lock: if it is another account nothing is written and the
transaction rolls back, so its lock is not needed; if nobody owns it, the
UNIQUE index on ``users."telegramChatId"`` is the arbiter (a concurrent claim
waits on the index and then fails with a unique violation). Locking the owner
as well would order two ``users`` rows by request content, and two accounts
claiming each other's chats would then deadlock.

Token values and deep links are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from enum import Enum

import asyncpg

logger = logging.getLogger(__name__)

# 32 random bytes, URL-safe base64 without padding (web: randomBytes(32).toString("base64url")).
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Prisma stores DateTime as UTC in timestamp-without-time-zone columns, so compare
# against UTC "now" explicitly instead of relying on the session TimeZone.
_NOW_UTC = "(now() AT TIME ZONE 'UTC')"

TOKEN_OWNER = 'SELECT "userId" FROM telegram_link_tokens WHERE "tokenHash" = $1'
LOCK_USER = "SELECT id FROM users WHERE id = $1 FOR UPDATE"

CONSUME_TOKEN = f"""
UPDATE telegram_link_tokens
   SET "consumedAt" = {_NOW_UTC}
 WHERE "tokenHash" = $1
   AND "userId" = $2
   AND "consumedAt" IS NULL
   AND "revokedAt" IS NULL
   AND "expiresAt" > {_NOW_UTC}
RETURNING id
"""

CHAT_OWNER = 'SELECT id FROM users WHERE "telegramChatId" = $1'  # no lock: see module docstring
LINK_CHAT = 'UPDATE users SET "telegramChatId" = $1 WHERE id = $2'

# Retry detection: a consumed token whose user already has this exact chat.
ALREADY_LINKED = """
SELECT 1
  FROM telegram_link_tokens t
  JOIN users u ON u.id = t."userId"
 WHERE t."tokenHash" = $1
   AND t."consumedAt" IS NOT NULL
   AND u."telegramChatId" = $2
"""


class LinkResult(str, Enum):
    LINKED = "linked"                    # token consumed, chat linked
    ALREADY_LINKED = "already_linked"    # retry/replay of a link that already succeeded for this chat
    INVALID = "invalid"                  # malformed, unknown, expired, consumed or revoked (indistinguishable)
    CHAT_IN_USE = "chat_in_use"          # chat belongs to another account; nothing changed


class _Rollback(Exception):
    def __init__(self, result: LinkResult):
        self.result = result


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_well_formed(token: str) -> bool:
    return bool(TOKEN_PATTERN.fullmatch(token))


async def consume_link_token(pool, token: str, chat_id: str) -> LinkResult:
    """Atomically consume ``token`` and link ``chat_id`` (a private chat) to its user."""
    if not is_well_formed(token):
        return LinkResult.INVALID
    token_hash = hash_token(token)
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                user_id = await conn.fetchval(TOKEN_OWNER, token_hash)
                if user_id is None:
                    raise _Rollback(LinkResult.INVALID)
                # Lock order step 1, then step 2 (see module docstring).
                if await conn.fetchval(LOCK_USER, user_id) is None:
                    raise _Rollback(LinkResult.INVALID)  # user (and its tokens) deleted meanwhile
                if await conn.fetchval(CONSUME_TOKEN, token_hash, user_id) is None:
                    raise _Rollback(LinkResult.INVALID)  # consumed, revoked or expired
                owner = await conn.fetchval(CHAT_OWNER, chat_id)
                if owner is not None and owner != user_id:
                    raise _Rollback(LinkResult.CHAT_IN_USE)  # token stays unconsumed
                if owner is None and await conn.execute(LINK_CHAT, chat_id, user_id) != "UPDATE 1":
                    raise _Rollback(LinkResult.INVALID)
        except _Rollback as rollback:
            if rollback.result is LinkResult.INVALID and await conn.fetchval(ALREADY_LINKED, token_hash, chat_id):
                return LinkResult.ALREADY_LINKED
            return rollback.result
        except asyncpg.UniqueViolationError:
            # Another account claimed this chat concurrently; the transaction rolled back.
            return LinkResult.CHAT_IN_USE
    return LinkResult.LINKED
