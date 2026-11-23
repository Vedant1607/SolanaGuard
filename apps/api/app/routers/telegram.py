import logging
from fastapi import APIRouter, Depends, Request
from app.auth import require_internal_secret, require_telegram_secret
from app.db import get_pool
from app.alerts.telegram import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()


# Telegram sends X-Telegram-Bot-Api-Secret-Token; requests are refused (fail closed)
# when TELEGRAM_WEBHOOK_SECRET is not configured (see app/auth.py).
@router.post("/webhook", dependencies=[Depends(require_telegram_secret)])
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            user_id = parts[1].strip()
            pool = await get_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    'UPDATE users SET "telegramChatId" = $1 WHERE id = $2',
                    str(chat_id), user_id,
                )
            if result == "UPDATE 1":
                await send_telegram_message(
                    chat_id,
                    "✅ Telegram alerts connected! You'll hear from me here when a "
                    "protocol you're watching escalates to HIGH or CRITICAL risk.",
                )
            else:
                await send_telegram_message(chat_id, "⚠️ Couldn't find your SolanaGuard account — try connecting again from the dashboard.")
        else:
            await send_telegram_message(chat_id, "👋 Open SolanaGuard and click \"Connect Telegram\" to link your account.")

    return {"ok": True}


# Operator-only: sends an arbitrary message, so it requires X-Internal-Api-Secret.
@router.post("/test-message", dependencies=[Depends(require_internal_secret)])
async def test_message(chat_id: str, text: str = "Test message from SolanaGuard"):
    sent = await send_telegram_message(chat_id, text)
    return {"sent": sent}