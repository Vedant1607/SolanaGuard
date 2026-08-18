import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)


async def send_alert_email(to_email: str, protocol_name: str, risk_level: str, score: float, explanation: str) -> bool:
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set, skipping email send")
        return False

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={
                    "from": settings.ALERT_FROM_EMAIL,
                    "to": [to_email],
                    "subject": f"⚠️ {protocol_name} risk level: {risk_level}",
                    "html": (
                        f"<p><strong>{protocol_name}</strong> risk score just hit "
                        f"<strong>{score}/100 ({risk_level})</strong>.</p><p>{explanation}</p>"
                        f"<p style='color:#888;font-size:12px'>You're receiving this because "
                        f"you're watching {protocol_name} on SolanaGuard.</p>"
                    ),
                },
            )
            if resp.status_code >= 400:
                logger.warning(f"Resend API error {resp.status_code}: {resp.text}")
                return False
            return True
        except Exception:
            logger.exception(f"Failed to send alert email to {to_email}")
            return False