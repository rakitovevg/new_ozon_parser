"""Парсер листинга Ozon через уже запущенный Chrome (GUI VPS) по CDP.

Имитация человеческого поведения (скролл), селекторы из конфига.
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
    REMOTE_CHROME_WS,
    USE_REMOTE_CHROME,
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
        model_words = model_filter.lower().split(" ")

    out: list[dict] = []
    seen_links: set[str] = set()
    count = 1

    for tile in tiles[: max(1, int(SELECTOR_MAX_CARDS or 100))]:
        try:
            count += 1
            logger.info(f"count: {count}")
            name_el = tile.select_one(SELECTOR_NAME_LINK) if SELECTOR_NAME_LINK else None
            if not name_el:
                continue
            link = name_el.nextSibling.get("href")
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

            name = name_el.nextSibling.getText()
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
    Делает переход на страницу, скроллит, забирает HTML и парсит его
    тем же кодом, что и ScrapingBee-путь (_parse_listing_html).
    """
    from playwright.sync_api import sync_playwright

    if not REMOTE_CHROME_WS:
        raise RuntimeError("REMOTE_CHROME_WS is not set")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(REMOTE_CHROME_WS)
        # используем уже существующий контекст браузера (с профилем и расширениями),
        # чтобы не создавать "чистый" новый профиль без расширений
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context()
        page = context.new_page()

        # Пытаемся навигироваться 3 раза — Ozon может делать промежуточные редиректы.
        for attempt in range(3):
            try:
                page.goto(url, wait_until="networkidle", timeout=SELECTOR_WAIT_TIMEOUT * 4000)
                break
            except Exception as nav_err:
                logger.warning("remote chrome: nav attempt %s failed: %s", attempt + 1, nav_err)
                if attempt == 2:
                    browser.close()
                    logger.error("remote chrome: navigation failed after 3 attempts")
                    return []
                page.wait_for_timeout(2000)

        # большая пауза, чтобы дорисовались все динамические блоки
        page.wait_for_timeout(8000)

        # ждём появления хотя бы одной карточки (не критично, если таймаут)
        try:
            page.wait_for_selector(SELECTOR_TILE_ROOT, timeout=SELECTOR_WAIT_TIMEOUT * 1000)
        except Exception as wait_err:
            logger.warning("remote chrome: wait_for_selector timed out: %s", wait_err)

        # скроллим страницу, чтобы догрузить карточки (до max_scrolls раз)
        max_cards = int(SELECTOR_MAX_CARDS or 100)
        max_scrolls = 10
        scrolls = 0
        last_html_len = 0

        while scrolls < max_scrolls:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight);")
            page.wait_for_timeout(random.uniform(3000, 4500))
            html_now = page.content()
            if len(html_now) == last_html_len:
                break
            last_html_len = len(html_now)
            scrolls += 1

        # забираем финальный HTML и закрываем только вкладку,
        # сам Chrome (GUI) оставляем работать
        html = page.content()
        page.close()

    # парсим HTML тем же кодом, что и раньше (ScrapingBee + Selenium путь)
    found = _parse_listing_html(html, min_price=min_price, model_filter=model_filter)

    # нотификации и callback'и — те же, что были в ScrapingBee-пути
    model_words = model_filter.lower().split(" ")
    for rec in found:
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
        # send_telegram_callback пробрасывается через внешнюю функцию,
        # поэтому здесь только found_products_callback
        if found_products_callback:
            found_products_callback(rec)

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
    Синхронный прогон: открывает url в уже запущенном Chrome (через CDP),
    скроллит страницу, парсит до SELECTOR_MAX_CARDS карточек.
    Для карточек с ценой <= min_price отправляет уведомление в Telegram и
    добавляет в found_products_callback.
    cancel_check_callback(task_id) -> True означает остановку.
    Возвращает список {"name", "price", "link"}.
    """
    found_products: list[dict] = []

    try:
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
            # Telegram уведомления — здесь, чтобы формат совпадал со старым кодом
            model_words = model_filter.lower().split(" ")
            for rec in found_products:
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
            return found_products

        raise RuntimeError("REMOTE_CHROME not configured")

    except Exception as e:
        logger.exception("run_parse_listing_sync: task_id=%s error %s", task_id, e)
        raise