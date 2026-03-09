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
import json
from pathlib import Path
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

# Маркеры страницы блокировки Ozon (антибот)
OZON_BLOCK_PAGE_MARKERS = ("Доступ ограничен", "доступ ограничен", "Инцидент:")


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

    # Флаги и настройки из окружения
    HEADLESS = os.getenv("CHROME_HEADLESS", "true").lower() == "true"
    DEBUG_SCREENSHOT = os.getenv("DEBUG_SCREENSHOT", "false").lower() == "true"
    USER_AGENT = os.getenv("CHROME_USER_AGENT") or ""
    USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR") or ""
    COOKIES_PATH = os.getenv("OZON_COOKIES_JSON") or ""
    # Локальный таймаут ожидания плиток (по умолчанию = SELECTOR_WAIT_TIMEOUT)
    try:
        WAIT_TIMEOUT = int(os.getenv("OZON_WAIT_TIMEOUT", str(SELECTOR_WAIT_TIMEOUT)))
    except ValueError:
        WAIT_TIMEOUT = SELECTOR_WAIT_TIMEOUT

    try:
        options = Options()
        # Языки и окно как у обычного пользователя
        options.add_argument("--lang=ru-RU")
        options.add_argument("--window-size=1920,1080")
        options.add_experimental_option(
            "prefs",
            {"intl.accept_languages": "ru-RU,ru"}
        )

        # Стелс-настройки
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation"],
        )
        options.add_experimental_option("useAutomationExtension", False)

        # Базовые флаги для сервера
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # options.add_argument("--disable-gpu")  # по желанию

        if HEADLESS:
            options.add_argument("--headless=new")

        if USER_AGENT:
            options.add_argument(f"user-agent={USER_AGENT}")

        if USER_DATA_DIR:
            options.add_argument(f"--user-data-dir={USER_DATA_DIR}")

        if proxy_url:
            proxy_dict = _get_proxy_for_selenium(proxy_url)
            if proxy_dict and proxy_dict.get("server"):
                options.add_argument(f"--proxy-server={proxy_dict['server']}")

        # Selenium Manager сам подберёт/скачает подходящий chromedriver под установленный Chrome.
        driver = webdriver.Chrome(options=options)

        driver.set_page_load_timeout(60)
        # Небольшая задержка «как человек»
        time.sleep(random.uniform(1.0, 2.5))

        # Если есть файл с куками, сначала заходим на базовый домен, ставим куки и только потом идём на нужный URL.
        # Так куки реально отправятся в запросе к листингу, а не просто добавятся в уже загруженную страницу.
        def _load_start_page_with_cookies():
            nonlocal driver
            if not COOKIES_PATH:
                driver.get(url)
                return

            cookies_file = Path(COOKIES_PATH)
            if not cookies_file.is_file():
                driver.get(url)
                return

            try:
                # Сначала открываем главную Ozon, чтобы домен совпал для add_cookie
                driver.get(OZON_BASE_URL)
                cookies = json.loads(cookies_file.read_text(encoding="utf-8"))
                for c in cookies:
                    try:
                        cookie = {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain", ".ozon.ru"),
                            "path": c.get("path", "/"),
                        }
                        if c.get("expiry") is not None:
                            cookie["expiry"] = c["expiry"]
                        if "secure" in c:
                            cookie["secure"] = c["secure"]
                        if "httpOnly" in c:
                            cookie["httpOnly"] = c["httpOnly"]
                        driver.add_cookie(cookie)
                    except Exception:
                        continue
                # Небольшая пауза и переход на нужный URL уже с куками
                time.sleep(random.uniform(0.5, 1.0))
                driver.get(url)
            except Exception:
                logger.exception("Не удалось загрузить куки из %s", COOKIES_PATH)
                driver.get(url)

        _load_start_page_with_cookies()

        # Отладочный скриншот
        if DEBUG_SCREENSHOT:
            screenshot_path = f"/tmp/ozon_task_{task_id}.png"
            try:
                driver.save_screenshot(screenshot_path)
                logger.info("Скриншот задачи %s сохранён в %s", task_id, screenshot_path)
            except Exception:
                logger.exception("Не удалось сохранить скриншот для задачи %s", task_id)

        # Проверка страницы «Доступ ограничен» (антибот Ozon)
        try:
            page_source = driver.page_source or ""
            if any(m in page_source for m in OZON_BLOCK_PAGE_MARKERS):
                blocked_screenshot = f"/tmp/ozon_task_{task_id}_blocked.png"
                blocked_html = f"/tmp/ozon_task_{task_id}_blocked.html"
                try:
                    driver.save_screenshot(blocked_screenshot)
                    logger.warning("Страница блокировки: скриншот сохранён в %s", blocked_screenshot)
                except Exception:
                    logger.exception("Не удалось сохранить скриншот блокировки для задачи %s", task_id)
                try:
                    Path(blocked_html).write_text(page_source, encoding="utf-8")
                    logger.warning("Страница блокировки: HTML сохранён в %s", blocked_html)
                except Exception:
                    logger.exception("Не удалось сохранить HTML блокировки для задачи %s", task_id)
                raise ValueError(
                    "Ozon: доступ ограничен (антибот). Смените IP/прокси или используйте резидентный прокси."
                )
        except ValueError:
            raise

        wait = WebDriverWait(driver, WAIT_TIMEOUT)
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
        # При таймауте делаем скриншот и логируем HTML для отладки
        from selenium.common.exceptions import TimeoutException

        if isinstance(e, TimeoutException) and driver:
            timeout_screenshot = f"/tmp/ozon_task_{task_id}_timeout.png"
            timeout_html = f"/tmp/ozon_task_{task_id}_timeout.html"
            try:
                driver.save_screenshot(timeout_screenshot)
                logger.info(
                    "Скриншот таймаута задачи %s сохранён в %s",
                    task_id,
                    timeout_screenshot,
                )
            except Exception:
                logger.exception(
                    "Не удалось сохранить скриншот таймаута для задачи %s", task_id
                )
            try:
                Path(timeout_html).write_text(driver.page_source, encoding="utf-8")
                logger.info(
                    "HTML таймаута задачи %s сохранён в %s",
                    task_id,
                    timeout_html,
                )
            except Exception:
                logger.exception(
                    "Не удалось сохранить HTML таймаута для задачи %s", task_id
                )

        logger.exception("run_parse_listing_sync: task_id=%s error %s", task_id, e)
        raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass