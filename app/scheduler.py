"""
Планировщик задач поиска: интервал / раз в сутки.
Запуск парсинга в потоке (Selenium sync), глобальный режим прокси подхватывается на лету.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import get_use_proxy_global
from app.database import async_session
from app.models import Brand, SearchTask, FoundProduct
from app.parser import build_search_url, run_parse_listing_sync
from app.telegram import send_telegram_message
from app.proxy_rotation import refresh_proxy_list, get_next_proxy_url

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
JOB_PREFIX = "search_task_"
JOB_GLOBAL_RUN_ALL = "global_run_all"
_executor = ThreadPoolExecutor(max_workers=2)

_cancel_requested: set[int] = set()
_cancel_lock = asyncio.Lock()


def request_cancel(task_id: int) -> None:
    _cancel_requested.add(task_id)


def is_cancel_requested(task_id: int) -> bool:
    return task_id in _cancel_requested


def clear_cancel(task_id: int) -> None:
    _cancel_requested.discard(task_id)


def _schedule_telegram(loop, msg: str) -> None:
    """Вызов из потока: планирует отправку в Telegram в основном loop."""
    asyncio.run_coroutine_threadsafe(send_telegram_message(msg), loop)


async def _save_found_product(task_id: int, rec: dict) -> None:
    async with async_session() as db:
        db.add(FoundProduct(task_id=task_id, name=rec["name"], price=rec["price"], link=rec["link"]))
        await db.commit()


def _found_products_callback_sync(task_id: int, loop, rec: dict) -> None:
    try:
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_save_found_product(task_id, rec), loop)
    except Exception:
        pass


async def run_search_task(task_id: int, from_scheduler: bool = False) -> None:
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        task = r.scalar_one_or_none()
        if not task:
            return
        if from_scheduler and not task.is_active:
            return
        brand_name = task.brand.strip()
        r2 = await db.execute(select(Brand).where(Brand.name == brand_name))
        brand = r2.scalar_one_or_none()
        if not brand:
            logger.warning("run_search_task: brand '%s' not found", brand_name)
            return
        url = build_search_url(brand.name, brand.code, task.model)
        if not url:
            logger.warning("run_search_task: empty url for task_id=%s", task_id)
            return

    clear_cancel(task_id)
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        t = r.scalar_one()
        t.run_status = "running"
        t.run_error = None
        await db.commit()

    use_proxy = get_use_proxy_global()
    proxy_url = get_next_proxy_url() if use_proxy else None
    if use_proxy and not proxy_url:
        logger.warning("run_search_task: use_proxy=True but no proxies in list")

    loop = asyncio.get_running_loop()
    min_price_float = float(task.min_price)

    def telegram_cb(msg: str) -> None:
        _schedule_telegram(loop, msg)

    def cancel_cb(tid: int) -> bool:
        return is_cancel_requested(tid)

    def found_cb(rec: dict) -> None:
        _found_products_callback_sync(task_id, loop, rec)

    try:
        await loop.run_in_executor(
            _executor,
            lambda: run_parse_listing_sync(
                url=url,
                min_price=min_price_float,
                proxy_url=proxy_url,
                send_telegram_callback=telegram_cb,
                task_id=task_id,
                cancel_check_callback=cancel_cb,
                found_products_callback=found_cb,
            ),
        )
        final_status = "cancelled" if is_cancel_requested(task_id) else "completed"
        run_error = None
    except Exception as e:
        logger.exception("run_search_task: task_id=%s error", task_id)
        final_status = "failed"
        run_error = str(e)
    clear_cancel(task_id)

    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        t = r.scalar_one()
        t.run_status = final_status
        t.run_error = run_error
        t.last_run_at = datetime.utcnow()
        await db.commit()
    logger.info("run_search_task: task_id=%s finished, status=%s", task_id, final_status)


async def run_all_active_tasks() -> None:
    """Запускает все активные задачи (вызывается по глобальному расписанию)."""
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.is_active == True))
        tasks = r.scalars().all()
    for task in tasks:
        if task.run_status == "running":
            continue
        asyncio.create_task(run_search_task(task.id, from_scheduler=True))
    if tasks:
        logger.info("run_all_active_tasks: started %s task(s)", len([t for t in tasks if t.run_status != "running"]))


async def refresh_scheduler() -> None:
    """Перечитывает глобальное расписание из БД и вешает одну job на запуск всех активных задач."""
    from app.models import Setting
    # Удаляем старую глобальную job
    try:
        scheduler.remove_job(JOB_GLOBAL_RUN_ALL)
    except Exception:
        pass
    # Читаем глобальное расписание из настроек
    async with async_session() as db:
        settings = {}
        for key in ("schedule_type", "schedule_interval_seconds", "schedule_daily_time"):
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            settings[key] = (row.value or "").strip() if row and row.value else ""
    schedule_type = settings.get("schedule_type") or ""
    schedule_interval_seconds = settings.get("schedule_interval_seconds") or ""
    schedule_daily_time = settings.get("schedule_daily_time") or ""
    try:
        interval_sec = int(schedule_interval_seconds) if schedule_interval_seconds else 0
    except ValueError:
        interval_sec = 0
    if schedule_type == "interval" and interval_sec >= 60:
        scheduler.add_job(
            run_all_active_tasks,
            "interval",
            seconds=interval_sec,
            id=JOB_GLOBAL_RUN_ALL,
            replace_existing=True,
        )
        logger.info("refresh_scheduler: global interval %s sec", interval_sec)
    elif schedule_type == "daily" and schedule_daily_time:
        parts = schedule_daily_time.strip().split(":")
        try:
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            h, m = 9, 0
        scheduler.add_job(
            run_all_active_tasks,
            "cron",
            hour=h,
            minute=m,
            id=JOB_GLOBAL_RUN_ALL,
            replace_existing=True,
        )
        logger.info("refresh_scheduler: global daily at %02d:%02d", h, m)
    else:
        logger.info("refresh_scheduler: no global schedule")
