"""Отправка уведомлений в Telegram через Bot API (личка или канал)."""
import asyncio
import logging
import httpx
from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def send_telegram_message(text: str) -> None:
    chat_id = (TELEGRAM_CHAT_ID or "").strip()
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 429:
                try:
                    data = r.json()
                    retry_after = data.get("parameters", {}).get("retry_after", 60)
                except Exception:
                    retry_after = 60
                logger.warning("Telegram rate limit (429), retry after %s sec", retry_after)
                await asyncio.sleep(retry_after)
                r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning("Telegram sendMessage: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("Telegram send_telegram_message: %s", e)
