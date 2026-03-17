"""Конфигурация приложения. Селекторы и URL из .env."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional

# При запуске из PyInstaller-бандла лаунчер задаёт OZON_PROJECT_ROOT (папка с бинарником).
# Иначе — корень проекта по расположению этого файла.
_base = os.getenv("OZON_PROJECT_ROOT")
BASE_DIR = Path(_base) if _base else Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'ozon.db'}")

# Telegram (для канала: ID вида -100xxxxxxxxxx или @username; бот должен быть админом канала)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

# URL поиска: итоговый = URL1 + brand.name + "-" + brand.code + URL2 + model
SEARCH_URL1 = os.getenv("SEARCH_URL1", "")
SEARCH_URL2 = os.getenv("SEARCH_URL2", "")

# Chrome (undetected-chromedriver)
CHROME_VERSION_MAIN = int(os.getenv("CHROME_VERSION_MAIN", "145") or "145")

# Селекторы страницы листинга Ozon (из .env)
SELECTOR_TILE_ROOT = os.getenv("SELECTOR_TILE_ROOT", ".tile-root")
SELECTOR_PRICE = os.getenv("SELECTOR_PRICE", ".c35_3_13-a6")
SELECTOR_NAME_LINK = os.getenv("SELECTOR_NAME_LINK", ".ki4_24")
SELECTOR_WAIT_TIMEOUT = int(os.getenv("SELECTOR_WAIT_TIMEOUT", "30") or "30")
SELECTOR_MAX_CARDS = int(os.getenv("SELECTOR_MAX_CARDS", "100") or "100")

# ScrapingBee (web scraping API). If API key is set, parser uses ScrapingBee instead of Selenium.
SCRAPINGBEE_API_KEY = (os.getenv("SCRAPINGBEE_API_KEY") or "").strip()
SCRAPINGBEE_RENDER_JS = (os.getenv("SCRAPINGBEE_RENDER_JS", "true") or "true").strip().lower() in ("1", "true", "yes", "y")
SCRAPINGBEE_STEALTH_PROXY = (os.getenv("SCRAPINGBEE_STEALTH_PROXY", "true") or "true").strip().lower() in ("1", "true", "yes", "y")
SCRAPINGBEE_COUNTRY_CODE = (os.getenv("SCRAPINGBEE_COUNTRY_CODE") or "ru").strip().lower()
SCRAPINGBEE_BLOCK_RESOURCES = (os.getenv("SCRAPINGBEE_BLOCK_RESOURCES", "false") or "false").strip().lower() in ("1", "true", "yes", "y")
SCRAPINGBEE_WAIT = int(os.getenv("SCRAPINGBEE_WAIT", "7000") or "7000")

# Remote Chrome (GUI VPS) via DevTools / CDP.
REMOTE_CHROME_WS = (os.getenv("REMOTE_CHROME_WS") or "").strip()
USE_REMOTE_CHROME = (os.getenv("USE_REMOTE_CHROME", "false") or "false").strip().lower() in ("1", "true", "yes", "y")

# Глобальный режим прокси (применяется ко всем задачам, читается на лету)
# Хранится в БД (таблица settings), здесь кэш для быстрого доступа
_use_proxy_global: Optional[bool] = None
_use_proxy_lock = threading.Lock()


def get_use_proxy_global() -> bool:
    """Читает глобальный флаг «использовать прокси» из кэша (обновляется из БД при каждом API)."""
    with _use_proxy_lock:
        if _use_proxy_global is None:
            return False
        return _use_proxy_global


def set_use_proxy_global(value: bool) -> None:
    with _use_proxy_lock:
        global _use_proxy_global
        _use_proxy_global = value
