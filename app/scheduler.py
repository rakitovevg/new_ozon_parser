"""
Планировщик задач поиска: интервал / раз в сутки.
Запуск парсинга в потоке (Selenium sync), глобальный режим прокси подхватывается на лету.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from app.config import (
    FAILED_TASK_RETRY_SECONDS,
    get_use_proxy_global,
    SCHEDULER_TIMEZONE,
    TASK_HARD_TIMEOUT_SECONDS,
)
from app.database import async_session
from app.models import Brand, SearchTask, FoundProduct
from app.parser import build_search_url, run_parse_listing_sync
from app.telegram import send_telegram_message
from app.proxy_rotation import refresh_proxy_list, get_next_proxy_url
from app.events import broadcaster

logger = logging.getLogger(__name__)

try:
    _scheduler_tz = ZoneInfo(SCHEDULER_TIMEZONE)
except Exception:
    logger.warning("scheduler: invalid SCHEDULER_TIMEZONE=%r, using UTC", SCHEDULER_TIMEZONE)
    _scheduler_tz = ZoneInfo("UTC")

scheduler = AsyncIOScheduler(
    timezone=_scheduler_tz,
    job_defaults={
        # если процесс был занят, cron не «проглатывать»; выполнить с задержкой
        "misfire_grace_time": 3600,
        "coalesce": False,
    },
)
JOB_PREFIX = "search_task_"
JOB_GLOBAL_RUN_ALL = "global_run_all"
JOB_RETRY_PREFIX = "retry_task_"
# Важно: один профиль/Chrome лучше не грузить параллельно.
_executor = ThreadPoolExecutor(max_workers=1)

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
        db.add(
            FoundProduct(
                task_id=task_id,
                name=rec["name"],
                price=rec["price"],
                link=rec["link"],
                stock=rec.get("stock"),
                revenue_30d=rec.get("revenue_30d"),
                orders_30d=rec.get("orders_30d"),
                rating=rec.get("rating"),
                reviews=rec.get("reviews"),
                promo=rec.get("promo"),
            )
        )
        await db.commit()


def _found_products_callback_sync(task_id: int, loop, rec: dict) -> None:
    try:
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_save_found_product(task_id, rec), loop)
    except Exception:
        pass


def cancel_pending_retries(task_id: int) -> None:
    """Снимает отложенный автоповтор после сбоя, если задачу запустили вручную или снова по расписанию."""
    prefix = f"{JOB_RETRY_PREFIX}{task_id}_"
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(prefix):
            try:
                scheduler.remove_job(job.id)
            except Exception:
                pass


def schedule_failed_task_retry(task_id: int) -> None:
    """Один повтор через FAILED_TASK_RETRY_SECONDS (новая job, старые pending на этот task_id снимаются)."""
    cancel_pending_retries(task_id)
    run_at = datetime.now(_scheduler_tz) + timedelta(seconds=FAILED_TASK_RETRY_SECONDS)
    job_id = f"{JOB_RETRY_PREFIX}{task_id}_{uuid.uuid4().hex[:12]}"
    scheduler.add_job(
        run_search_task,
        DateTrigger(run_date=run_at),
        args=[task_id],
        kwargs={"from_scheduler": False, "retry_after_failure": True},
        id=job_id,
        replace_existing=False,
    )
    logger.info(
        "schedule_failed_task_retry: task_id=%s scheduled at %s (%s sec)",
        task_id,
        run_at.isoformat(),
        FAILED_TASK_RETRY_SECONDS,
    )


async def run_search_task(
    task_id: int,
    from_scheduler: bool = False,
    retry_after_failure: bool = False,
) -> None:
    cancel_pending_retries(task_id)
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        task = r.scalar_one_or_none()
        if not task:
            return
        if retry_after_failure and not task.is_active:
            logger.info("run_search_task: task_id=%s retry skipped (task inactive)", task_id)
            return
        if from_scheduler and not task.is_active:
            return

        # Новый источник URL — поле task.url. Для старых задач, где url может быть пустым,
        # сохраняем обратную совместимость через build_search_url.
        url = (task.url or "").strip()
        if not url:
            brand_name = (task.brand or "").strip()
            if not brand_name:
                logger.warning("run_search_task: task_id=%s has no url and empty brand", task_id)
                return
            r2 = await db.execute(select(Brand).where(Brand.name == brand_name))
            brand = r2.scalar_one_or_none()
            if not brand:
                logger.warning("run_search_task: brand '%s' not found", brand_name)
                return
            url = build_search_url(brand.name, brand.code, task.model)
            if not url:
                logger.warning("run_search_task: empty url (fallback) for task_id=%s", task_id)
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
    model_filter = (task.model or "").strip()

    def telegram_cb(msg: str) -> None:
        _schedule_telegram(loop, msg)

    def cancel_cb(tid: int) -> bool:
        return is_cancel_requested(tid)

    def found_cb(rec: dict) -> None:
        _found_products_callback_sync(task_id, loop, rec)

    error_for_telegram: str | None = None

    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                _executor,
                lambda: run_parse_listing_sync(
                    url=url,
                    min_price=min_price_float,
                    proxy_url=proxy_url,
                    send_telegram_callback=telegram_cb,
                    task_id=task_id,
                    cancel_check_callback=cancel_cb,
                    found_products_callback=found_cb,
                    model_filter=model_filter,
                ),
            ),
            timeout=TASK_HARD_TIMEOUT_SECONDS,
        )
        final_status = "cancelled" if is_cancel_requested(task_id) else "completed"
        run_error = None
    except asyncio.TimeoutError:
        logger.error(
            "run_search_task: task_id=%s hard timeout after %s seconds",
            task_id,
            TASK_HARD_TIMEOUT_SECONDS,
        )
        request_cancel(task_id)
        final_status = "failed"
        run_error = f"Timeout after {TASK_HARD_TIMEOUT_SECONDS} seconds"
        error_for_telegram = run_error
    except Exception as e:
        logger.exception("run_search_task: task_id=%s error", task_id)
        final_status = "failed"
        run_error = str(e)
        error_for_telegram = run_error
    clear_cancel(task_id)

    if final_status == "failed":
        try:
            schedule_failed_task_retry(task_id)
        except Exception:
            logger.exception("run_search_task: schedule_failed_task_retry failed for task_id=%s", task_id)

    # Если была ошибка, отправляем краткое уведомление в Telegram (без падения при сбое отправки).
    if error_for_telegram:
        try:
            msg = (
                f"⚠️ Ошибка задачи #{task_id}\n"
                f"Статус: {final_status}\n"
                f"{error_for_telegram[:1500]}"
            )
            await send_telegram_message(msg)
        except Exception:
            logger.exception("run_search_task: failed to send error to Telegram for task_id=%s", task_id)

    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        t = r.scalar_one()
        t.run_status = final_status
        t.run_error = run_error
        t.last_run_at = datetime.utcnow()
        await db.commit()
    logger.info("run_search_task: task_id=%s finished, status=%s", task_id, final_status)
    try:
        await broadcaster.publish(
            "task_finished",
            {"task_id": task_id, "status": final_status, "ts": datetime.utcnow().isoformat()},
        )
    except Exception:
        pass


async def run_all_active_tasks() -> None:
    """Запускает все активные задачи по очереди (один парсер в executor — без лавины create_task)."""
    async with async_session() as db:
        r = await db.execute(
            select(SearchTask.id).where(SearchTask.is_active == True).order_by(SearchTask.id),
        )
        task_ids = [row[0] for row in r.all()]
    for tid in task_ids:
        async with async_session() as db:
            r2 = await db.execute(select(SearchTask).where(SearchTask.id == tid))
            t = r2.scalar_one_or_none()
            if not t or not t.is_active or t.run_status == "running":
                continue
        await run_search_task(tid, from_scheduler=True)
    logger.info("run_all_active_tasks: finished queue (%s id(s))", len(task_ids))


async def refresh_scheduler() -> None:
    """Перечитывает глобальное расписание из БД и вешает одну job на запуск всех активных задач."""
    from app.models import Setting
    # Удаляем старые глобальные jobs (может быть несколько при "daily" с несколькими временами)
    try:
        for job in scheduler.get_jobs():
            if job.id == JOB_GLOBAL_RUN_ALL or job.id.startswith(f"{JOB_GLOBAL_RUN_ALL}_"):
                scheduler.remove_job(job.id)
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
        # Поддержка списка времени через запятую:
        # "09:00" или "09:00, 13:30, 18:45"
        raw_times = [p.strip() for p in (schedule_daily_time or "").split(",") if p.strip()]
        parsed_times: list[tuple[int, int]] = []
        for raw in raw_times:
            parts = raw.split(":")
            try:
                h = int(parts[0])
                m = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                logger.warning("refresh_scheduler: invalid daily time token %r", raw)
                continue
            if not (0 <= h <= 23 and 0 <= m <= 59):
                logger.warning("refresh_scheduler: daily time out of range %r", raw)
                continue
            parsed_times.append((h, m))

        if parsed_times:
            for h, m in parsed_times:
                scheduler.add_job(
                    run_all_active_tasks,
                    "cron",
                    hour=h,
                    minute=m,
                    id=f"{JOB_GLOBAL_RUN_ALL}_{h:02d}{m:02d}",
                    replace_existing=True,
                )
            logger.info(
                "refresh_scheduler: global daily at %s",
                ", ".join([f"{h:02d}:{m:02d}" for h, m in parsed_times]),
            )
        else:
            logger.info("refresh_scheduler: no valid daily times in %r", schedule_daily_time)
    else:
        logger.info("refresh_scheduler: no global schedule")
