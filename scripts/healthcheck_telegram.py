#!/usr/bin/env python3
"""
Внешний healthcheck: если HTTP-эндпоинт не отвечает — шлёт Telegram.
Работает даже когда упало само приложение (запуск по cron/systemd timer).

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — обязательны для отправки
  HEALTHCHECK_URL — URL проверки (по умолчанию http://127.0.0.1:8000/)
  HEALTHCHECK_STATE_FILE — файл состояния (JSON), по умолчанию /tmp/ozon_parser_health.json
  HEALTHCHECK_TIMEOUT_SEC — таймаут запроса, по умолчанию 10
  HEALTHCHECK_REPEAT_SEC — повторять алерт пока сервис лежит, каждые N сек (0 = только первый раз)
  HEALTHCHECK_RECOVER_NOTIFY — 1/true: уведомить при восстановлении после падения
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_ok": True, "last_fail_notify_ts": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "last_ok" not in data:
            data["last_ok"] = True
        data.setdefault("last_fail_notify_ts", 0.0)
        return data
    except Exception:
        return {"last_ok": True, "last_fail_notify_ts": 0.0}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _send_telegram(text: str) -> bool:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skip notify", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return False


def _http_ok(url: str, timeout: float) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            if 200 <= code < 400:
                return True, ""
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    url = (os.getenv("HEALTHCHECK_URL") or "http://127.0.0.1:8000/").strip()
    state_path = Path(os.getenv("HEALTHCHECK_STATE_FILE") or "/tmp/ozon_parser_health.json")
    timeout = float(os.getenv("HEALTHCHECK_TIMEOUT_SEC") or "10")
    repeat_sec = float(os.getenv("HEALTHCHECK_REPEAT_SEC") or "3600")
    recover = (os.getenv("HEALTHCHECK_RECOVER_NOTIFY") or "true").strip().lower() in ("1", "true", "yes", "y")

    ok, err = _http_ok(url, timeout)
    state = _load_state(state_path)
    now = time.time()
    last_ok = bool(state.get("last_ok", True))
    last_fail_notify = float(state.get("last_fail_notify_ts", 0))

    if ok:
        if not last_ok and recover:
            host = os.uname().nodename if hasattr(os, "uname") else "server"
            _send_telegram(f"✅ <b>Ozon Parser</b> снова доступен.\n\n<code>{url}</code>\n🖥 {host}")
        state["last_ok"] = True
        state["last_fail_notify_ts"] = 0.0
        _save_state(state_path, state)
        return 0

    # down
    should_notify = last_ok or (
        repeat_sec > 0 and (now - last_fail_notify) >= repeat_sec
    )
    if should_notify:
        host = os.uname().nodename if hasattr(os, "uname") else "server"
        msg = (
            f"🚨 <b>Ozon Parser недоступен</b>\n\n"
            f"<code>{url}</code>\n"
            f"Ошибка: <code>{err or 'no response'}</code>\n"
            f"🖥 {host}"
        )
        if _send_telegram(msg):
            state["last_fail_notify_ts"] = now
    state["last_ok"] = False
    _save_state(state_path, state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
