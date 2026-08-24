import logging
from fastapi import APIRouter, Request, Header, HTTPException
from app.db import get_pool
from app.config import settings
from app.alerts.telegram import send_telegram_message

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

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


@router.post("/test-message")
async def test_message(chat_id: str, text: str = "Test message from SolanaGuard"):
    sent = await send_telegram_message(chat_id, text)
    return {"sent": sent}