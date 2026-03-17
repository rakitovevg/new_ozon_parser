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
    SCRAPINGBEE_API_KEY,
    SCRAPINGBEE_RENDER_JS,
    SCRAPINGBEE_STEALTH_PROXY,
    SCRAPINGBEE_COUNTRY_CODE,
    SCRAPINGBEE_BLOCK_RESOURCES,
    SCRAPINGBEE_WAIT,
    REMOTE_CHROME_WS,
    USE_REMOTE_CHROME,
)

logger = logging.getLogger(__name__)

OZON_BASE_URL = "https://www.ozon.ru"

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"


def build_search_url(brand_name: str, brand_code: str, model: str) -> str:
    """URL = URL1 + brand.name + '-' + brand.code + URL2 + model."""
    if not SEARCH_URL1 or not SEARCH_URL2:
        return ""
    name = (brand_name or "").strip().replace(" ", "-")
    code = (brand_code or "").strip()
    model_clean = (model or "").strip()
    return f"{SEARCH_URL1}{name}-{code}{SEARCH_URL2}{model_clean}"


def _fetch_html_via_scrapingbee(url: str) -> str:
    """
    Fetches rendered HTML using ScrapingBee.
    Uses settings from config.py / .env.
    """
    if not SCRAPINGBEE_API_KEY:
        raise RuntimeError("SCRAPINGBEE_API_KEY is not set")
    import httpx

    params = {
        "api_key": SCRAPINGBEE_API_KEY,
        "url": url,
        "render_js": "true" if SCRAPINGBEE_RENDER_JS else "false",
        "country_code": SCRAPINGBEE_COUNTRY_CODE or "ru",
    }
    # Используем stealth_proxy (он у тебя работает), premium_proxy не трогаем.
    if SCRAPINGBEE_STEALTH_PROXY:
        params["stealth_proxy"] = "true"
    if SCRAPINGBEE_BLOCK_RESOURCES:
        params["block_resources"] = "true"
    if SCRAPINGBEE_WAIT and SCRAPINGBEE_WAIT > 0:
        params["wait"] = str(SCRAPINGBEE_WAIT)

    # For stability: slightly higher timeouts (Ozon pages can be heavy)
    timeout = httpx.Timeout(90.0, connect=30.0)
    with httpx.Client(timeout=timeout, headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"}) as client:
        r = client.get(SCRAPINGBEE_ENDPOINT, params=params)
        r.raise_for_status()
        return r.text


def _parse_listing_html(html: str, min_price: float, model_filter: Optional[str]) -> list[dict]:
    """
    Parses listing HTML and returns list of {"name","price","link"}.
    Uses the same CSS selectors as Selenium-path (from config/.env).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    tiles = soup.select(SELECTOR_TILE_ROOT) if SELECTOR_TILE_ROOT else []
    if not tiles:
        return []

    model_words: list[str] = []
    if model_filter:
        model_words = [w for w in model_filter.lower().split(" ") if w]

    out: list[dict] = []
    seen_links: set[str] = set()

    for tile in tiles[: max(1, int(SELECTOR_MAX_CARDS or 100))]:
        try:
            name_el = tile.select_one(SELECTOR_NAME_LINK) if SELECTOR_NAME_LINK else None
            if not name_el:
                continue
            link = (name_el.get("href") or "").strip()
            if link and not link.startswith("http"):
                link = urljoin(OZON_BASE_URL, link)
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            price_el = tile.select_one(SELECTOR_PRICE) if SELECTOR_PRICE else None
            price_text = (price_el.get_text(" ", strip=True) if price_el else "") or ""
            price = int(re.sub(r"\D", "", price_text)) if price_text else 0
            if price <= 0 or price > min_price:
                continue

            name = (name_el.get_text(" ", strip=True) or "").strip() or "—"
            if model_words:
                name_lower = name.lower()
                if not all(word in name_lower for word in model_words):
                    continue

            out.append({"name": name, "price": price, "link": link})
        except Exception:
            continue

    out.sort(key=lambda x: x["price"])
    return out


def _scrape_with_remote_chrome(
    url: str,
    min_price: float,
    task_id: int,
    cancel_check_callback,
    found_products_callback,
    model_filter: Optional[str],
) -> list[dict]:
    """
    Использует уже запущенный Chrome (GUI VPS) через DevTools / CDP.
    Требует REMOTE_CHROME_WS и USE_REMOTE_CHROME=true.
    """
    from playwright.sync_api import sync_playwright

    if not REMOTE_CHROME_WS:
        raise RuntimeError("REMOTE_CHROME_WS is not set")

    found: list[dict] = []
    model_words = [w for w in (model_filter or "").lower().split() if w]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(REMOTE_CHROME_WS)
        page = browser.new_page()
        # Пытаемся навигироваться 3 раза — Ozon может делать промежуточные редиректы.
        for attempt in range(3):
            try:
                page.goto(url, wait_until="networkidle", timeout=120_000)
                break
            except Exception as nav_err:
                logger.warning("remote chrome: nav attempt %s failed: %s", attempt + 1, nav_err)
                if attempt == 2:
                    browser.close()
                    logger.error("remote chrome: navigation failed after 3 attempts")
                    return []
                page.wait_for_timeout(2000)

        # небольшая пауза, чтобы дорисовались динамические блоки
        page.wait_for_timeout(1500)

        # Поиск карточек с защитой от повторной навигации
        for attempt in range(3):
            try:
                tiles = page.query_selector_all(SELECTOR_TILE_ROOT)
                break
            except Exception as q_err:
                logger.warning("remote chrome: query_selector_all attempt %s failed: %s", attempt + 1, q_err)
                tiles = []
                page.wait_for_timeout(2000)

        for tile in tiles[: max(1, int(SELECTOR_MAX_CARDS or 100))]:
            if cancel_check_callback and cancel_check_callback(task_id):
                break
            try:
                name_el = tile.query_selector(SELECTOR_NAME_LINK)
                price_el = tile.query_selector(SELECTOR_PRICE)
                if not name_el or not price_el:
                    continue

                link = (name_el.get_attribute("href") or "").strip()
                if link and not link.startswith("http"):
                    link = urljoin(OZON_BASE_URL, link)
                if not link:
                    continue

                price_text = (price_el.inner_text() or "").strip()
                price = int(re.sub(r"\D", "", price_text)) if price_text else 0
                if price <= 0 or price > min_price:
                    continue

                name = (name_el.inner_text() or "").strip() or "—"
                if model_words:
                    name_lower = name.lower()
                    if not all(word in name_lower for word in model_words):
                        continue

                rec = {"name": name, "price": price, "link": link}
                found.append(rec)
                if found_products_callback:
                    found_products_callback(rec)
            except Exception:
                continue

        browser.close()

    found.sort(key=lambda x: x["price"])
    logger.info("run_parse_listing_sync (remote chrome): task_id=%s, подходящих товаров=%d", task_id, len(found))
    return found


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
    model_filter: Optional[str] = None,
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
        # 1) Remote Chrome (GUI VPS) — если включен
        if USE_REMOTE_CHROME and REMOTE_CHROME_WS:
            logger.info("run_parse_listing_sync: task_id=%s using remote Chrome", task_id)
            found_products = _scrape_with_remote_chrome(
                url=url,
                min_price=min_price,
                task_id=task_id,
                cancel_check_callback=cancel_check_callback,
                found_products_callback=found_products_callback,
                model_filter=model_filter,
            )
            return found_products

        # 2) ScrapingBee (облачный HTML), если задан API-ключ
        if SCRAPINGBEE_API_KEY:
            logger.info("run_parse_listing_sync: task_id=%s using ScrapingBee", task_id)
            html = _fetch_html_via_scrapingbee(url)
            found_products = _parse_listing_html(html, min_price=min_price, model_filter=model_filter)
            # Telegram notifications + callbacks (keep original behaviour)
            model_words = [w for w in (model_filter or "").lower().split(" ") if w]
            for rec in found_products:
                if cancel_check_callback and cancel_check_callback(task_id):
                    break
                name = rec["name"]
                price = rec["price"]
                link = rec["link"]
                if model_words:
                    name_lower = name.lower()
                    if not all(word in name_lower for word in model_words):
                        continue
                msg = (
                    f"🔥 <b>Цена снижена!</b>\n\n"
                    f"📦 {name}\n"
                    f"💰 Цена: {price} ₽\n"
                    f"🔗 <a href=\"{link}\">Купить на Ozon</a>"
                )
                if send_telegram_callback:
                    send_telegram_callback(msg)
                if found_products_callback:
                    found_products_callback(rec)
            logger.info("run_parse_listing_sync: task_id=%s, подходящих товаров=%d (ScrapingBee)", task_id, len(found_products))
            return found_products

        # 3) Локальный Selenium (fallback)
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

        # Подготовим слова фильтра модели (если заданы)
        model_words: list[str] = []
        if model_filter:
            model_words = model_filter.split(" ")

        max_cards = SELECTOR_MAX_CARDS  # сколько карточек просмотреть (первые N)
        max_scrolls = 10
        scrolls = 0
        seen_links: set[str] = set()  # уникальные карточки, которые уже учли
        last_items_count = 0

        # Скроллим и смотрим карточки, пока не просмотрим max_cards уникальных или не кончится контент
        while len(seen_links) < max_cards and scrolls <= max_scrolls:
            items = driver.find_elements(By.CSS_SELECTOR, SELECTOR_TILE_ROOT)
            items_count = len(items)
            new_cards_this_round = 0

            for item in items:
                if cancel_check_callback and cancel_check_callback(task_id):
                    break
                try:
                    name_el = item.find_element(By.CSS_SELECTOR, SELECTOR_NAME_LINK)
                    link = name_el.get_attribute("href") or ""
                    if link and not link.startswith("http"):
                        link = urljoin(OZON_BASE_URL, link)
                    if not link or link in seen_links:
                        continue
                    seen_links.add(link)
                    new_cards_this_round += 1

                    price_el = item.find_element(By.CSS_SELECTOR, SELECTOR_PRICE)
                    price_text = price_el.text
                    price = int(re.sub(r"\D", "", price_text)) if price_text else 0
                    if price <= 0 or price > min_price:
                        continue

                    name = (name_el.text or "").strip() or "—"
                    if model_words:
                        name_lower = name.lower()
                        if not all(word in name_lower for word in model_words):
                            continue

                    # Подходит по цене и модели — добавляем в результат
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

            time.sleep(random.uniform(3, 4.5))        
            if cancel_check_callback and cancel_check_callback(task_id):
                logger.info("run_parse_listing_sync: task_id=%s cancelled", task_id)
                break
            if len(seen_links) >= max_cards:
                logger.info("run_parse_listing_sync: task_id=%s max_cards reached", task_id)
                break
            if scrolls > 0 and (new_cards_this_round == 0 or items_count == last_items_count):
                logger.info("run_parse_listing_sync: task_id=%s no new cards found", task_id)
                break

            last_items_count = items_count
            driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
            scrolls += 1

        logger.info("run_parse_listing_sync: task_id=%s просмотренных товаров=%d", task_id, len(seen_links))
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