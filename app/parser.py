"""
Парсер листинга Ozon через Selenium (undetected-chromedriver).
Имитация человеческого поведения, селекторы из конфига.
Запускается в потоке (sync), вызывается из asyncio через run_in_executor.
"""
from __future__ import annotations

import os
import re
import time
import random
import logging
from urllib.parse import urljoin
from typing import Optional

from app.config import (
    SEARCH_URL1,
    SEARCH_URL2,
    SELECTOR_TILE_ROOT,
    SELECTOR_PRICE,
    SELECTOR_NAME_LINK,
    SELECTOR_WAIT_TIMEOUT,
    SELECTOR_MAX_CARDS,
)

logger = logging.getLogger(__name__)

OZON_BASE_URL = "https://www.ozon.ru"


def build_search_url(brand_name: str, brand_code: str, model: str) -> str:
    """URL = URL1 + brand.name + '-' + brand.code + URL2 + model."""
    if not SEARCH_URL1 or not SEARCH_URL2:
        return ""
    name = (brand_name or "").strip().replace(" ", "-")
    code = (brand_code or "").strip()
    model_clean = (model or "").strip()
    return f"{SEARCH_URL1}{name}-{code}{SEARCH_URL2}{model_clean}"


def _get_proxy_for_selenium(proxy_url: Optional[str]) -> Optional[dict]:
    """Преобразует URL прокси в строку для --proxy-server."""
    if not proxy_url or not proxy_url.strip():
        return None
    from urllib.parse import urlparse

    raw = proxy_url.strip()
    u = urlparse(raw)

    # Если пользователь ввёл уже host:port — используем как есть
    if not u.scheme and ":" in raw and "@" not in raw:
        return {"server": raw}

    scheme = (u.scheme or "").lower()
    host = u.hostname or ""
    # для HTTP-прокси без порта по умолчанию 80, для socks5 — 1080
    port = u.port or (1080 if scheme.startswith("socks") else 80)

    if scheme.startswith("socks"):
        # Для SOCKS Chrome ожидает схему
        server = f"{scheme}://{host}:{port}"
    else:
        # Для обычного HTTP/HTTPS — только host:port
        server = f"{host}:{port}"

    return {"server": server}


def run_parse_listing_sync(
    url: str,
    min_price: float,
    proxy_url: Optional[str],
    send_telegram_callback,
    task_id: int,
    cancel_check_callback,
    found_products_callback,
) -> list[dict]:
    """
    Синхронный прогон: открывает url в Chrome, ждёт .tile-root, обрабатывает до SELECTOR_MAX_CARDS карточек.
    Для карточек с ценой <= min_price отправляет уведомление в Telegram и добавляет в found_products_callback.
    cancel_check_callback(task_id) -> True означает остановку.
    Возвращает список {"name", "price", "link"}.
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options

    driver = None
    found_products: list[dict] = []

    # Флаги из окружения
    HEADLESS = os.getenv("CHROME_HEADLESS", "true").lower() == "true"
    DEBUG_SCREENSHOT = os.getenv("DEBUG_SCREENSHOT", "false").lower() == "true"

    try:
        options = Options()
        options.add_argument("--lang=ru-RU")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # options.add_argument("--disable-gpu")  # по желанию
        if HEADLESS:
            options.add_argument("--headless=new")

        if proxy_url:
            proxy_dict = _get_proxy_for_selenium(proxy_url)
            if proxy_dict and proxy_dict.get("server"):
                options.add_argument(f"--proxy-server={proxy_dict['server']}")

        # Selenium Manager сам подберёт/скачает подходящий chromedriver под установленный Chrome.
        driver = webdriver.Chrome(options=options)

        driver.set_page_load_timeout(60)
        # Небольшая задержка «как человек»
        time.sleep(random.uniform(1.0, 2.5))
        driver.get(url)

        # Отладочные дампы (по желанию)
        if DEBUG_SCREENSHOT:
            screenshot_path = f"/tmp/ozon_task_{task_id}.png"
            try:
                driver.save_screenshot(screenshot_path)
                logger.info("Скриншот задачи %s сохранён в %s", task_id, screenshot_path)
            except Exception:
                logger.exception("Не удалось сохранить скриншот для задачи %s", task_id)

        wait = WebDriverWait(driver, SELECTOR_WAIT_TIMEOUT)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELECTOR_TILE_ROOT)))
        time.sleep(random.uniform(0.5, 1.5))
        items = driver.find_elements(By.CSS_SELECTOR, SELECTOR_TILE_ROOT)
        items = items[:SELECTOR_MAX_CARDS]

        for item in items:
            if cancel_check_callback and cancel_check_callback(task_id):
                break
            try:
                price_el = item.find_element(By.CSS_SELECTOR, SELECTOR_PRICE)
                price_text = price_el.text
                price = int(re.sub(r"\D", "", price_text)) if price_text else 0
                if price <= 0:
                    continue
                if price > min_price:
                    continue
                name_el = item.find_element(By.CSS_SELECTOR, SELECTOR_NAME_LINK)
                name = (name_el.text or "").strip() or "—"
                link = name_el.get_attribute("href") or ""
                if link and not link.startswith("http"):
                    link = urljoin(OZON_BASE_URL, link)
                msg = (
                    f"🔥 <b>Цена снижена!</b>\n\n"
                    f"📦 {name}\n"
                    f"💰 Цена: {price} ₽\n"
                    f"🔗 <a href=\"{link}\">Купить на Ozon</a>"
                )
                if send_telegram_callback:
                    send_telegram_callback(msg)
                rec = {"name": name, "price": price, "link": link}
                found_products.append(rec)
                if found_products_callback:
                    found_products_callback(rec)
            except Exception:
                continue

        found_products.sort(key=lambda x: x["price"])
        logger.info("run_parse_listing_sync: task_id=%s, подходящих товаров=%d", task_id, len(found_products))
        return found_products

    except Exception as e:
        logger.exception("run_parse_listing_sync: task_id=%s error %s", task_id, e)
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass