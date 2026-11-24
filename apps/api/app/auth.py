"""Shared-secret checks for internal endpoints and the Telegram webhook.

Both fail closed: with no secret configured on the server every request is
refused (503), and a missing or wrong header is refused (401). Comparisons use
``hmac.compare_digest``. Secrets travel only in headers, never in the URL, and
are never logged or echoed in responses.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

INTERNAL_SECRET_HEADER = "X-Internal-Api-Secret"
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def secret_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison; never true for an empty expected secret or a missing header."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_internal_secret(
    x_internal_api_secret: str | None = Header(default=None, alias=INTERNAL_SECRET_HEADER),
) -> None:
    """Dependency for operator-only endpoints (ingestion trigger, test alerts)."""
    if not settings.INTERNAL_API_SECRET:
        logger.warning("Internal endpoint refused: INTERNAL_API_SECRET is not configured")
        raise HTTPException(status_code=503, detail="Internal endpoints are not configured on this server")
    if not secret_matches(x_internal_api_secret, settings.INTERNAL_API_SECRET):
        raise HTTPException(status_code=401, detail="Missing or invalid internal API secret")


async def require_telegram_secret(
    x_telegram_bot_api_secret_token: str | None = Header(default=None, alias=TELEGRAM_SECRET_HEADER),
) -> None:
    """Dependency for the Telegram webhook (Telegram sends the secret set with setWebhook)."""
    if not settings.TELEGRAM_WEBHOOK_SECRET:
        logger.warning("Telegram webhook refused: TELEGRAM_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured on this server")
    if not secret_matches(x_telegram_bot_api_secret_token, settings.TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
