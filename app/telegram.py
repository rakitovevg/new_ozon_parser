"""Отправка уведомлений в Telegram через Bot API (личка или канал)."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# Docker / сети с фильтрами: до api.telegram.org иногда долго идёт TCP → ConnectTimeout
_TELEGRAM_TIMEOUT = httpx.Timeout(
    float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "60") or "60"),
    connect=float(os.getenv("TELEGRAM_HTTP_CONNECT_TIMEOUT", "45") or "45"),
)
_TELEGRAM_RETRIES = max(1, int(os.getenv("TELEGRAM_HTTP_RETRIES", "3") or "3"))

_TRANSIENT = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.NetworkError,
)


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

    for attempt in range(1, _TELEGRAM_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT) as client:
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
            return
        except _TRANSIENT as e:
            if attempt >= _TELEGRAM_RETRIES:
                logger.error(
                    "Telegram send failed after %s attempts (%s): %s",
                    _TELEGRAM_RETRIES,
                    type(e).__name__,
                    e,
                )
                return
            delay = 2.0 * attempt
            logger.warning(
                "Telegram attempt %s/%s: %s — retry in %.1fs",
                attempt,
                _TELEGRAM_RETRIES,
                type(e).__name__,
                delay,
            )
            await asyncio.sleep(delay)
        except Exception as e:
            logger.exception("Telegram send_telegram_message: %s", e)
            return
