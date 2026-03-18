from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator


class EventBroadcaster:
    """
    Простой in-memory broadcaster для Server-Sent Events.
    Подходит для одного процесса uvicorn.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._queues.add(q)
        try:
            # hello + keepalive
            yield self._format("hello", {"ts": time.time()})
            last_ping = time.monotonic()
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield msg
                except asyncio.TimeoutError:
                    # keepalive ping, чтобы соединение не отваливалось на прокси
                    now = time.monotonic()
                    if now - last_ping >= 25.0:
                        last_ping = now
                        yield self._format("ping", {"ts": time.time()})
        finally:
            async with self._lock:
                self._queues.discard(q)

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = self._format(event, data)
        async with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # если клиент не читает — пропускаем
                pass

    @staticmethod
    def _format(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


broadcaster = EventBroadcaster()

