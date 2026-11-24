import logging
from fastapi import APIRouter, Depends, Request
from app.auth import require_internal_secret, require_telegram_secret
from app.db import get_pool
from app.alerts.telegram import send_telegram_message
from app.telegram_linking import LinkResult, consume_link_token

logger = logging.getLogger(__name__)
router = APIRouter()


MSG_LINKED = ("✅ Telegram alerts connected! You'll hear from me here when a "
              "protocol you're watching escalates to HIGH or CRITICAL risk.")
MSG_ALREADY_LINKED = "✅ This chat is already connected to your SolanaGuard account."
MSG_INVALID = ("⚠️ This link is invalid or has expired. Open SolanaGuard and click "
               "\"Connect Telegram\" to get a new one.")
MSG_CHAT_IN_USE = ("⚠️ This Telegram chat is already connected to a different SolanaGuard "
                   "account. Nothing was changed.")
MSG_HELP = "👋 Open SolanaGuard and click \"Connect Telegram\" to link your account."
REPLIES = {LinkResult.LINKED: MSG_LINKED, LinkResult.ALREADY_LINKED: MSG_ALREADY_LINKED,
           LinkResult.INVALID: MSG_INVALID, LinkResult.CHAT_IN_USE: MSG_CHAT_IN_USE}


def _start_payload(text: str) -> str | None:
    """The argument of a /start command (``/start`` or ``/start@Bot``), "" if none; None if not /start."""
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    command = parts[0]
    if command != "/start" and not command.startswith("/start@"):
        return None
    return parts[1].strip() if len(parts) == 2 else ""


# Telegram sends X-Telegram-Bot-Api-Secret-Token; requests are refused (fail closed)
# when TELEGRAM_WEBHOOK_SECRET is not configured. Handled updates always answer
# 200 so Telegram does not redeliver them; a database error answers 500 (after
# rolling back), and Telegram's redelivery is then safe because nothing was
# committed. A redelivered, already-applied /start is recognised (ALREADY_LINKED).
@router.post("/webhook", dependencies=[Depends(require_telegram_secret)])
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return {"ok": True}
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(text, str):
        return {"ok": True}
    payload = _start_payload(text)
    if payload is None:
        return {"ok": True}
    # Only private chats can be linked: in a group or channel the chat ID is shared,
    # and a deep link pasted there must not subscribe everyone. Ignored silently.
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
        return {"ok": True}
    chat_id = chat["id"]

    if not payload:
        await send_telegram_message(chat_id, MSG_HELP)
        return {"ok": True}

    result = await consume_link_token(await get_pool(), payload, str(chat_id))
    logger.info("Telegram link attempt: %s", result.value)  # never the token or the link
    await send_telegram_message(chat_id, REPLIES[result])
    return {"ok": True}


# Operator-only: sends an arbitrary message, so it requires X-Internal-Api-Secret.
@router.post("/test-message", dependencies=[Depends(require_internal_secret)])
async def test_message(chat_id: str, text: str = "Test message from SolanaGuard"):
    sent = await send_telegram_message(chat_id, text)
    return {"sent": sent}