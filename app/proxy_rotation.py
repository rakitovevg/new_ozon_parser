"""Ротация прокси из таблицы proxies."""
from __future__ import annotations

import threading
from typing import List, Optional

from sqlalchemy import select
from app.database import async_session
from app.models import Proxy

_proxy_urls: List[str] = []
_proxy_index = 0
_lock = threading.Lock()


async def refresh_proxy_list() -> None:
    """Загружает список URL прокси из БД."""
    global _proxy_urls
    async with async_session() as db:
        r = await db.execute(select(Proxy.url).order_by(Proxy.id))
        rows = r.scalars().all()
    with _lock:
        _proxy_urls = [row[0] for row in rows if row and row[0] and str(row[0]).strip()]


def get_next_proxy_url() -> Optional[str]:
    """Следующий прокси по кругу. Вызывать из любого потока."""
    with _lock:
        if not _proxy_urls:
            return None
        global _proxy_index
        url = _proxy_urls[_proxy_index % len(_proxy_urls)]
        _proxy_index += 1
        return url
