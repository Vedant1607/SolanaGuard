import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)


async def send_telegram_message(chat_id: str, text: str) -> bool:
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set, skipping Telegram send")
        return False

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            if resp.status_code >= 400:
                logger.warning(f"Telegram API error {resp.status_code}: {resp.text}")
                return False
            return True
        except Exception:
            logger.exception(f"Failed to send Telegram message to {chat_id}")
            return False