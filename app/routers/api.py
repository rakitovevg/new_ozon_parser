from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.models import Brand, FoundProduct, Proxy, SearchTask
from app.proxy_rotation import refresh_proxy_list
from app.schemas import ScheduleBody, SearchTaskCreate, SearchTaskUpdate, UseProxyBody
from app.scheduler import refresh_scheduler, request_cancel, run_search_task
from app.services.settings_service import (
    get_cached_use_proxy,
    get_schedule_settings,
    set_schedule_settings,
    set_use_proxy,
    sync_use_proxy_from_db,
)

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/brands", response_model=list)
async def api_brands(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Brand).order_by(Brand.name))
    brands = r.scalars().all()
    return [{"id": b.id, "name": b.name, "code": b.code} for b in brands]


@router.get("/settings/use-proxy")
async def api_get_use_proxy(db: AsyncSession = Depends(get_db)):
    await sync_use_proxy_from_db()
    return {"use_proxy": get_cached_use_proxy()}


@router.post("/settings/use-proxy")
async def api_set_use_proxy(body: UseProxyBody):
    value = await set_use_proxy(body.use_proxy)
    return {"ok": True, "use_proxy": value}


@router.get("/settings/schedule")
async def api_get_schedule():
    s = await get_schedule_settings()
    return {
        "schedule_type": s["schedule_type"],
        "schedule_interval_seconds": s["schedule_interval_seconds"],
        "schedule_daily_time": s["schedule_daily_time"],
    }


@router.post("/settings/schedule")
async def api_set_schedule(body: ScheduleBody):
    await set_schedule_settings(
        schedule_type=body.schedule_type,
        schedule_interval_seconds=body.schedule_interval_seconds,
        schedule_daily_time=body.schedule_daily_time,
    )
    await refresh_scheduler()
    return {
        "ok": True,
        "schedule_type": body.schedule_type,
        "schedule_interval_seconds": body.schedule_interval_seconds,
        "schedule_daily_time": body.schedule_daily_time,
    }


@router.get("/search-tasks", response_model=list)
async def api_search_tasks_list(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).order_by(SearchTask.created_at.desc()))
    tasks = r.scalars().all()
    return [
        {
            "id": t.id,
            "url": t.url,
            "brand": t.brand,
            "model": t.model,
            "min_price": t.min_price,
            "is_active": t.is_active,
            "schedule_type": t.schedule_type,
            "schedule_interval_seconds": t.schedule_interval_seconds,
            "schedule_daily_time": t.schedule_daily_time,
            "run_status": t.run_status,
            "run_error": t.run_error,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]


@router.post("/search-tasks")
async def api_search_task_create(
    body: SearchTaskCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    task = SearchTask(
        brand=(body.brand or "").strip(),
        model=(body.model or "").strip(),
        url=body.url.strip(),
        min_price=float(body.min_price),
        is_active=body.is_active,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    if body.run_now:
        background_tasks.add_task(run_search_task, task.id)
    return {"id": task.id, "message": "Задача создана"}


@router.patch("/search-tasks/{task_id}")
async def api_search_task_update(task_id: int, body: SearchTaskUpdate, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if body.url is not None:
        task.url = body.url.strip()
    if body.min_price is not None:
        task.min_price = float(body.min_price)
    if body.brand is not None:
        task.brand = body.brand.strip()
    if body.model is not None:
        task.model = body.model.strip()
    if body.is_active is not None:
        task.is_active = body.is_active
    await db.commit()
    await refresh_scheduler()
    return {"ok": True}


@router.delete("/search-tasks/{task_id}")
async def api_search_task_delete(task_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Задача не найдена")
    await db.delete(task)
    await db.commit()
    await refresh_scheduler()
    return {"ok": True}


@router.post("/search-tasks/{task_id}/run")
async def api_search_task_run(task_id: int, background_tasks: BackgroundTasks):
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if task.run_status == "running":
        raise HTTPException(400, "Задача уже выполняется")
    background_tasks.add_task(run_search_task, task_id)
    return {"ok": True, "message": "Запущено"}


@router.post("/search-tasks/{task_id}/stop")
async def api_search_task_stop(task_id: int):
    request_cancel(task_id)
    return {"ok": True, "message": "Остановка запрошена"}


@router.post("/search-tasks/run-all")
async def api_search_tasks_run_all(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.is_active == True))
    tasks = r.scalars().all()
    not_running = [t for t in tasks if t.run_status != "running"]
    for t in not_running:
        background_tasks.add_task(run_search_task, t.id)
    return {"ok": True, "started": len(not_running), "message": f"Запущено задач: {len(not_running)}"}


@router.get("/found-products", response_model=list)
async def api_found_products(
    skip: int = 0,
    limit: int = 200,
    task_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(FoundProduct).order_by(FoundProduct.created_at.desc()).offset(skip).limit(limit)
    if task_id is not None:
        q = q.where(FoundProduct.task_id == task_id)
    r = await db.execute(q)
    products = r.scalars().all()
    return [
        {
            "id": p.id,
            "task_id": p.task_id,
            "name": p.name,
            "price": p.price,
            "link": p.link,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in products
    ]


@router.get("/proxies", response_model=list)
async def api_proxies_list(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Proxy).order_by(Proxy.id))
    proxies = r.scalars().all()
    return [{"id": p.id, "url": p.url} for p in proxies]


@router.post("/proxies")
async def api_proxies_upload(body: dict, db: AsyncSession = Depends(get_db)):
    urls = body.get("urls", [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    for u in urls:
        if u:
            db.add(Proxy(url=u.strip()))
    await db.commit()
    await refresh_proxy_list()
    return {"ok": True, "added": len(urls)}


@router.delete("/proxies")
async def api_proxies_clear(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Proxy))
    await db.commit()
    await refresh_proxy_list()
    return {"ok": True}
