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
import html
import httpx
from urllib.parse import urljoin
from typing import Optional
from io import BytesIO

from app.config import (
    SEARCH_URL1,
    SEARCH_URL2,
    SELECTOR_TILE_ROOT,
    SELECTOR_PRICE,
    SELECTOR_NAME_LINK,
    SELECTOR_WAIT_TIMEOUT,
    SELECTOR_MAX_CARDS,
    REMOTE_CHROME_WS,
    REMOTE_CHROME_HTTP,
    USE_REMOTE_CHROME,
)

logger = logging.getLogger(__name__)

OZON_BASE_URL = "https://www.ozon.ru"


def _digits_price(raw: str) -> int:
    if not raw:
        return 0
    try:
        return int(re.sub(r"\D", "", raw)) or 0
    except Exception:
        return 0


def _parse_price_int_from_tile(tile) -> int:
    """
    Цена в карточке листинга. Сначала SELECTOR_PRICE из .env, затем блок c35_3_15-a0
    (первый tsHeadline500Medium — актуальная цена; второй span — старая), далее запасные варианты.
    """
    if SELECTOR_PRICE and str(SELECTOR_PRICE).strip():
        try:
            el = tile.select_one(str(SELECTOR_PRICE).strip())
            if el:
                v = _digits_price(el.get_text(" ", strip=True))
                if v > 0:
                    return v
        except Exception:
            logger.debug("price: SELECTOR_PRICE %r failed", SELECTOR_PRICE, exc_info=True)

    for sel in (
        ".c35_3_15-a0 span.tsHeadline500Medium",
        "div.c35_3_15-a0 > span.tsHeadline500Medium",
        ".c35_3_15-a0 > span.tsHeadline500Medium",
        "span.tsHeadline500Medium.c35_3_13-a1",
        "span.tsHeadline500Medium",
        "span[class*='tsHeadline500Medium']",
        "span[class*='tsHeadline'][class*='Medium']",
    ):
        try:
            el = tile.select_one(sel)
            if el:
                v = _digits_price(el.get_text(" ", strip=True))
                if v >= 10:
                    return v
        except Exception:
            continue

    for el in tile.find_all("span"):
        t = el.get_text(" ", strip=True) or ""
        if "₽" in t or "руб" in t.lower():
            v = _digits_price(t)
            if v > 0:
                return v

    for el in tile.select("span[class*='Headline'], span[class*='headline']"):
        v = _digits_price(el.get_text(" ", strip=True))
        if v >= 100:
            return v

    blob = tile.get_text(" ", strip=True) or ""
    m = re.search(r"([\d\s\u00a0\u202f]{2,})\s*₽", blob)
    if m:
        return _digits_price(m.group(1))
    return 0


def _is_ucenka_marked_text(text: str | None) -> bool:
    """Уценка в произвольном тексте: название карточки или акция из mpstats."""
    if not text or not str(text).strip():
        return False
    n = str(text).lower().replace("ё", "е")
    if "уценка" in n:
        return True
    if "уценен" in n:
        return True
    if "уценнен" in n:
        return True
    return False


def _resolve_remote_chrome_ws() -> str:
    """
    Resolve current Chrome DevTools websocket URL.
    Priority:
    1) REMOTE_CHROME_WS if provided (can still be valid after restarts)
    2) REMOTE_CHROME_HTTP/json/version -> webSocketDebuggerUrl
    """
    if REMOTE_CHROME_WS:
        return REMOTE_CHROME_WS

    base = (REMOTE_CHROME_HTTP or "").rstrip("/")
    if not base:
        raise RuntimeError("REMOTE_CHROME_HTTP is not set")

    url = f"{base}/json/version"
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise RuntimeError(f"failed to read Chrome DevTools endpoint {url}: {e}") from e

    ws = (data.get("webSocketDebuggerUrl") or "").strip()
    if not ws:
        raise RuntimeError(f"webSocketDebuggerUrl is empty in {url}")
    return ws


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

    # Сначала собираем карту SKU -> метрики из таблицы расширения (если она есть)
    # Поля: остаток, выручка, заказы, рейтинг, отзывы, акция
    sku_metrics: dict[str, dict] = {}
    try:
        rows = soup.select("tr._tr_ysl04_1")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue
            # колонка с SKU — третья (индекс 2)
            sku_cell = cells[2]
            sku_span = sku_cell.find("span")
            if not sku_span:
                continue
            sku_text = (sku_span.get_text(strip=True) or "").strip()
            if not sku_text:
                continue
            # колонка с остатком — шестая (индекс 5)
            stock_cell = cells[5]
            stock_text = (stock_cell.get_text(" ", strip=True) or "").strip()
            # выручка за 30 дней — седьмая (индекс 6)
            revenue_cell = cells[6] if len(cells) > 6 else None
            revenue_text = (revenue_cell.get_text(" ", strip=True) if revenue_cell else "").strip()
            # заказов — восьмая (индекс 7)
            orders_cell = cells[7] if len(cells) > 7 else None
            orders_text = (orders_cell.get_text(" ", strip=True) if orders_cell else "").strip()
            # рейтинг — девятая (индекс 8)
            rating_cell = cells[8] if len(cells) > 8 else None
            rating_text = (rating_cell.get_text(" ", strip=True) if rating_cell else "").strip()
            # количество отзывов — десятая (индекс 9)
            reviews_cell = cells[9] if len(cells) > 9 else None
            reviews_text = (reviews_cell.get_text(" ", strip=True) if reviews_cell else "").strip()
            # акция — одиннадцатая (индекс 10)
            promo_cell = cells[10] if len(cells) > 10 else None
            promo_text = (promo_cell.get_text(" ", strip=True) if promo_cell else "").strip()

            def _to_int(raw: str) -> int | None:
                raw = (raw or "").strip()
                if not raw:
                    return None
                try:
                    return int(re.sub(r"\D", "", raw))
                except Exception:
                    return None

            stock_value = _to_int(stock_text)
            revenue_value = _to_int(revenue_text)
            orders_value = _to_int(orders_text)
            reviews_value = _to_int(reviews_text)

            # рейтинг может быть с точкой, поэтому отдельно
            rating_value: float | None
            try:
                rating_clean = rating_text.replace(",", ".").strip()
                rating_value = float(rating_clean) if rating_clean else None
            except Exception:
                rating_value = None

            sku_metrics[sku_text] = {
                "stock": stock_value,
                "revenue_30d": revenue_value,
                "orders_30d": orders_value,
                "rating": rating_value,
                "reviews": reviews_value,
                "promo": promo_text or None,
            }
    except Exception:
        sku_metrics = {}

    tiles = soup.select(SELECTOR_TILE_ROOT) if SELECTOR_TILE_ROOT else []
    if not tiles:
        return []

    model_words: list[str] = []
    if model_filter:
        model_words = model_filter.lower().split(" ")

    out: list[dict] = []
    count = 0

    for tile in tiles[: max(1, int(SELECTOR_MAX_CARDS or 100))]:
        try:
            count += 1
            logger.info(f"tile #{count}")
            name_el = tile.select_one('span.tsBody500Medium')
            if not name_el:
                logger.info(f"tile #{count}: пропуск — не найден SELECTOR_NAME_LINK={SELECTOR_NAME_LINK!r}")
                continue

            raw_href = tile.select_one('a[href^="/product/"]')
            if raw_href:
                link = urljoin(OZON_BASE_URL, raw_href['href'])
            else:
                link = raw_href or ""
                logger.info(f"link = {link}")
            # если ссылки нет, всё равно учитываем товар (link = "")

            # пытаемся вытащить SKU из ссылки и найти остаток в таблице расширения
            sku = None
            try:
                # берём последнюю "длинную" цифробуквенную группу из URL как SKU
                m = re.findall(r"(\d{6,})", link)
                if m:
                    sku = m[-1]
            except Exception:
                sku = None

            # даже если по SKU нет метрик, просто берём пустой словарь
            metrics = sku_metrics.get(sku) or {}

            price = _parse_price_int_from_tile(tile)

            # аккуратно достаём name/link из соседнего узла, он может быть None
            #sibling = name_el.nextSibling
            #if not sibling:
            #    logger.info(f"tile #{count}: пропуск — отсутствует nextSibling у name_el")
            #    continue
            #if not hasattr(sibling, "getText"):
            #    logger.info(f"tile #{count}: пропуск — nextSibling без getText()")
            #    continue
            name = name_el.getText()
            if _is_ucenka_marked_text(name):
                logger.info(f"tile #{count}: пропуск — в названии уценка")
                continue
            promo_mpstats = metrics.get("promo")
            if promo_mpstats is not None and _is_ucenka_marked_text(str(promo_mpstats)):
                logger.info(f"tile #{count}: пропуск — акция mpstats (уценка)")
                continue

            logger.info(
                "tile #%s: price=%s, name=%s, sku=%s, model_words=%s",
                count,
                price,
                name,
                sku,
                model_words,
            )
            if price <= 0:
                logger.info(f"tile #{count}: пропуск — price <= 0 ({price})")
                continue
            if price > min_price:
                logger.info(f"tile #{count}: пропуск — price {price} > min_price {min_price}")
                continue

            if model_words:
                name_lower = name.lower()
                if not all(word in name_lower for word in model_words):
                    logger.info(f"tile #{count}: пропуск — не прошёл фильтр по model_words")
                    continue

            # best-effort: пытаемся найти продавца/магазин в карточке
            shop: str | None = None
            try:
                seller_a = tile.select_one('a[href*="/seller/"]')
                if seller_a:
                    shop = seller_a.get_text(" ", strip=True) or None
                if not shop:
                    # иногда продавец/магазин может быть в ссылке на магазин
                    shop_a2 = tile.select_one('a[href*="seller"]')
                    if shop_a2:
                        shop = shop_a2.get_text(" ", strip=True) or None
            except Exception:
                shop = None

            out.append(
                {
                    "name": name,
                    "price": price,
                    "link": link,
                    "stock": metrics.get("stock"),
                    "revenue_30d": metrics.get("revenue_30d"),
                    "orders_30d": metrics.get("orders_30d"),
                    "rating": metrics.get("rating"),
                    "reviews": metrics.get("reviews"),
                    "promo": metrics.get("promo"),
                    "shop": shop,
                },
            )
        except Exception as e:
            logger.exception("tile #%s: ошибка при разборе карточки: %s", count, e)
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
    Требует USE_REMOTE_CHROME=true и доступ к DevTools endpoint.
    Делает переход на страницу, скроллит, забирает HTML и парсит его
    тем же кодом, что и ScrapingBee-путь (_parse_listing_html).
    """
    from playwright.sync_api import sync_playwright

    def _extract_seller_from_product_page(p) -> str | None:
        # Захардкоженный селектор продавца (как ты просил — без SELLER_TAG)
        seller_selector = 'span.b35_3_30-b7[style*="-webkit-line-clamp"]'
        try:
            p.wait_for_selector(seller_selector, timeout=8000)
        except Exception:
            logger.info("seller: селектор %r не найден на странице товара", seller_selector)
            return None
        try:
            txt = p.evaluate('''() => {
            const span = document.querySelector('span.b35_3_30-b7[style*="-webkit-line-clamp"]');
            return span ? span.innerText.trim() : null;
        }''')
            
            if txt:
                logger.info("seller: найден продавец %r по селектору %r", txt, seller_selector)
                return txt
            logger.info("seller: селектор %r найден, но текст пустой", seller_selector)
            return None
        except Exception as e:
            logger.info("seller: ошибка чтения текста по селектору %r: %s", seller_selector, e)
            return None

    with sync_playwright() as p:
        ws_endpoint = _resolve_remote_chrome_ws()
        try:
            browser = p.chromium.connect_over_cdp(ws_endpoint)
        except Exception:
            # Stale ws endpoint after Chrome restart: refresh via /json/version.
            if REMOTE_CHROME_WS:
                base = (REMOTE_CHROME_HTTP or "").rstrip("/")
                if not base:
                    raise
                url = f"{base}/json/version"
                with httpx.Client(timeout=8.0) as client:
                    r = client.get(url)
                    r.raise_for_status()
                    data = r.json()
                ws_endpoint = (data.get("webSocketDebuggerUrl") or "").strip()
                if not ws_endpoint:
                    raise RuntimeError(f"webSocketDebuggerUrl is empty in {url}")
                browser = p.chromium.connect_over_cdp(ws_endpoint)
            else:
                raise
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

        # скроллим основную страницу, чтобы mpstats дорисовал все строки в таблице
        target_rows = int(SELECTOR_MAX_CARDS or 100)
        max_scrolls = max(80, target_rows)  # запас по скроллам
        scrolls = 0
        last_sku_count = 0
        no_growth = 0

        def _count_unique_skus() -> int:
            try:
                return int(
                    page.evaluate(
                        """
                        () => {
                          const spans = Array.from(document.querySelectorAll('tr._tr_ysl04_1 td:nth-child(3) span'));
                          const skus = spans.map(s => (s.innerText || '').trim()).filter(Boolean);
                          return new Set(skus).size;
                        }
                        """
                    )
                )
            except Exception:
                return 0

        # Скроллим выдачу вниз (как руками), чтобы mpstats догрузил строки.
        while scrolls < max_scrolls:
            page.evaluate("window.scrollBy(0, window.innerHeight);")
            if no_growth >= 5:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            page.wait_for_timeout(random.uniform(1800, 2600))

            sku_count = _count_unique_skus()
            logger.info("remote chrome: mpstats unique_sku_count=%s after scroll #%s", sku_count, scrolls + 1)

            if sku_count >= target_rows:
                break

            if sku_count <= last_sku_count:
                no_growth += 1
            else:
                last_sku_count = sku_count
                no_growth = 0

            # защита от зацикливания: если долго нет роста — принудительно вниз->вверх и проверка
            if no_growth >= 12:
                logger.info("remote chrome: mpstats no growth (%s) — force bottom->top cycle", no_growth)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(3500)
                page.evaluate("window.scrollTo(0, 0);")
                page.wait_for_timeout(3000)
                sku_after_cycle = _count_unique_skus()
                logger.info(
                    "remote chrome: mpstats unique_sku_count=%s after force bottom->top cycle",
                    sku_after_cycle,
                )
                if sku_after_cycle <= last_sku_count:
                    logger.info("remote chrome: mpstats still no growth — stop scrolling")
                    break
                last_sku_count = sku_after_cycle
                no_growth = 0

            scrolls += 1

        # Ключевой шаг: вернуться к таблице наверх и дать mpstats дорисовать DOM полностью
        page.evaluate("window.scrollTo(0, 0);")
        page.wait_for_timeout(2500)
        sku_count_top = _count_unique_skus()
        logger.info("remote chrome: mpstats unique_sku_count=%s after scroll back to top", sku_count_top)

        # забираем финальный HTML и закрываем только вкладку,
        # сам Chrome (GUI) оставляем работать
        html = page.content()
        # парсим HTML пока ещё подключены к браузеру (потом дособерём продавцов)
        found = _parse_listing_html(html, min_price=min_price, model_filter=model_filter)

        # добираем продавца только для подходящих товаров (обычно их мало)
        for rec in found:
            if rec.get("shop"):
                continue
            link = (rec.get("link") or "").strip()
            if not link:
                continue
            try:
                logger.info("seller: открываем страницу товара для продавца: %s", link)
                p2 = context.new_page()
                p2.goto(link, wait_until="domcontentloaded", timeout=SELECTOR_WAIT_TIMEOUT * 4000)
                p2.wait_for_timeout(1500)
                seller = _extract_seller_from_product_page(p2)
                if seller:
                    rec["shop"] = seller
                p2.close()
            except Exception:
                try:
                    p2.close()
                except Exception:
                    pass
                continue

        page.close()

    # callback для сохранения найденных товаров (уведомления отправляются выше по стеку)
    for rec in found:
        if cancel_check_callback and cancel_check_callback(task_id):
            break
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
        # REMOTE_CHROME_WS необязателен: актуальный ws берётся из REMOTE_CHROME_HTTP/json/version
        if USE_REMOTE_CHROME:
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
            for rec in found_products:
                name = rec["name"]
                price = rec["price"]
                stock = rec.get("stock")
                shop = rec.get("shop")
                link = (rec.get("link") or "").strip()
                safe_name = html.escape(str(name))
                safe_shop = html.escape(str(shop)) if shop else "—"
                safe_link = html.escape(link, quote=True)

                lines = [
                    f"📦 {safe_name}",
                    f"💰 Цена: {price} ₽",
                    f"📦 Остаток: {stock if stock is not None else '—'}",
                    f"🏪 Магазин: {safe_shop}",
                ]
                if link:
                    lines.append(f'🔗 <a href="{safe_link}">Открыть товар</a>')
                else:
                    lines.append("🔗 Ссылка: —")
                msg = "\n".join(lines)
                if send_telegram_callback:
                    send_telegram_callback(msg)
            return found_products

        raise RuntimeError(
            "REMOTE_CHROME not configured: set USE_REMOTE_CHROME=true "
            "and optionally REMOTE_CHROME_HTTP (e.g. http://127.0.0.1:9222) in .env"
        )

    except Exception as e:
        logger.exception("run_parse_listing_sync: task_id=%s error %s", task_id, e)
        raise