"""FastAPI: API и админка (вкладки Задачи, Найденные товары, Прокси)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR, set_use_proxy_global, get_use_proxy_global
from app.database import get_db, init_db, async_session
from app.models import Brand, SearchTask, FoundProduct, Setting, Proxy
from app.scheduler import scheduler, refresh_scheduler, run_search_task, request_cancel
from app.proxy_rotation import refresh_proxy_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

(BASE_DIR / "data").mkdir(exist_ok=True)


async def _sync_use_proxy_from_db():
    """Читает настройку use_proxy из БД и обновляет кэш в config."""
    async with async_session() as db:
        r = await db.execute(select(Setting).where(Setting.key == "use_proxy"))
        row = r.scalar_one_or_none()
    if row and row.value:
        set_use_proxy_global(row.value.strip().lower() in ("1", "true", "yes"))
    else:
        set_use_proxy_global(False)


async def _get_schedule_settings():
    """Читает глобальное расписание из БД (для всех задач)."""
    async with async_session() as db:
        out = {}
        for key in ("schedule_type", "schedule_interval_seconds", "schedule_daily_time"):
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            out[key] = (row.value or "").strip() if row and row.value else ""
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _sync_use_proxy_from_db()
    await refresh_proxy_list()
    if not scheduler.running:
        scheduler.start()
    await refresh_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="New Ozon Parser", lifespan=lifespan)

templates_dir = BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["getattr"] = getattr


# --- API: бренды ---

@app.get("/api/brands", response_model=list)
async def api_brands(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Brand).order_by(Brand.name))
    brands = r.scalars().all()
    return [{"id": b.id, "name": b.name, "code": b.code} for b in brands]


# --- API: настройки (use_proxy глобальный) ---

@app.get("/api/settings/use-proxy")
async def api_get_use_proxy(db: AsyncSession = Depends(get_db)):
    await _sync_use_proxy_from_db()
    return {"use_proxy": get_use_proxy_global()}


class UseProxyBody(BaseModel):
    use_proxy: bool


@app.post("/api/settings/use-proxy")
async def api_set_use_proxy(body: UseProxyBody, db: AsyncSession = Depends(get_db)):
    async with async_session() as db2:
        r = await db2.execute(select(Setting).where(Setting.key == "use_proxy"))
        row = r.scalar_one_or_none()
        if not row:
            db2.add(Setting(key="use_proxy", value="true" if body.use_proxy else "false"))
        else:
            row.value = "true" if body.use_proxy else "false"
        await db2.commit()
    set_use_proxy_global(body.use_proxy)
    return {"ok": True, "use_proxy": body.use_proxy}


# --- API: глобальное расписание ---

@app.get("/api/settings/schedule")
async def api_get_schedule():
    s = await _get_schedule_settings()
    return {"schedule_type": s["schedule_type"], "schedule_interval_seconds": s["schedule_interval_seconds"], "schedule_daily_time": s["schedule_daily_time"]}


class ScheduleBody(BaseModel):
    schedule_type: Optional[str] = None
    schedule_interval_seconds: Optional[int] = None
    schedule_daily_time: Optional[str] = None


@app.post("/api/settings/schedule")
async def api_set_schedule(body: ScheduleBody):
    async with async_session() as db:
        for key, val in (
            ("schedule_type", (body.schedule_type or "").strip() or None),
            ("schedule_interval_seconds", str(body.schedule_interval_seconds) if body.schedule_interval_seconds is not None else ""),
            ("schedule_daily_time", (body.schedule_daily_time or "").strip() or None),
        ):
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            value = val or ""
            if not row:
                db.add(Setting(key=key, value=value))
            else:
                row.value = value
        await db.commit()
    await refresh_scheduler()
    return {"ok": True, "schedule_type": body.schedule_type, "schedule_interval_seconds": body.schedule_interval_seconds, "schedule_daily_time": body.schedule_daily_time}


# --- API: задачи поиска ---

class SearchTaskCreate(BaseModel):
    brand: str
    model: str
    min_price: float
    is_active: bool = True
    run_now: bool = False


@app.get("/api/search-tasks", response_model=list)
async def api_search_tasks_list(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).order_by(SearchTask.created_at.desc()))
    tasks = r.scalars().all()
    return [
        {
            "id": t.id,
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


@app.post("/api/search-tasks")
async def api_search_task_create(body: SearchTaskCreate, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    task = SearchTask(
        brand=body.brand.strip(),
        model=body.model.strip(),
        min_price=float(body.min_price),
        is_active=body.is_active,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    if body.run_now:
        background_tasks.add_task(run_search_task, task.id)
    return {"id": task.id, "message": "Задача создана"}


@app.patch("/api/search-tasks/{task_id}")
async def api_search_task_update(task_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if "is_active" in body:
        task.is_active = body["is_active"]
    await db.commit()
    await refresh_scheduler()
    return {"ok": True}


@app.delete("/api/search-tasks/{task_id}")
async def api_search_task_delete(task_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Задача не найдена")
    await db.delete(task)
    await db.commit()
    await refresh_scheduler()
    return {"ok": True}


@app.post("/api/search-tasks/{task_id}/run")
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


@app.post("/api/search-tasks/{task_id}/stop")
async def api_search_task_stop(task_id: int):
    request_cancel(task_id)
    return {"ok": True, "message": "Остановка запрошена"}


@app.post("/api/search-tasks/run-all")
async def api_search_tasks_run_all(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.is_active == True))
    tasks = r.scalars().all()
    running = [t for t in tasks if t.run_status != "running"]
    for t in running:
        background_tasks.add_task(run_search_task, t.id)
    return {"ok": True, "started": len(running), "message": f"Запущено задач: {len(running)}"}


# --- API: найденные товары ---

@app.get("/api/found-products", response_model=list)
async def api_found_products(skip: int = 0, limit: int = 200, task_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    q = select(FoundProduct).order_by(FoundProduct.created_at.desc()).offset(skip).limit(limit)
    if task_id is not None:
        q = q.where(FoundProduct.task_id == task_id)
    r = await db.execute(q)
    products = r.scalars().all()
    return [
        {"id": p.id, "task_id": p.task_id, "name": p.name, "price": p.price, "link": p.link, "created_at": p.created_at.isoformat() if p.created_at else None}
        for p in products
    ]


# --- API: прокси ---

@app.get("/api/proxies", response_model=list)
async def api_proxies_list(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Proxy).order_by(Proxy.id))
    proxies = r.scalars().all()
    return [{"id": p.id, "url": p.url} for p in proxies]


@app.post("/api/proxies")
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


@app.delete("/api/proxies")
async def api_proxies_clear(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Proxy))
    await db.commit()
    await refresh_proxy_list()
    return {"ok": True}


# --- Админка: одна страница с вкладками ---

@app.get("/", response_class=HTMLResponse)
async def admin_index(request: Request, db: AsyncSession = Depends(get_db)):
    await _sync_use_proxy_from_db()
    r = await db.execute(select(SearchTask).order_by(SearchTask.created_at.desc()))
    tasks = r.scalars().all()
    r2 = await db.execute(select(Brand).order_by(Brand.name))
    brands = r2.scalars().all()
    r3 = await db.execute(select(Proxy).order_by(Proxy.id))
    proxies = r3.scalars().all()
    r4 = await db.execute(select(FoundProduct).order_by(FoundProduct.created_at.desc()).limit(50))
    found_products = r4.scalars().all()
    schedule = await _get_schedule_settings()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tasks": tasks,
            "brands": brands,
            "use_proxy": get_use_proxy_global(),
            "proxies": proxies,
            "found_products": found_products,
            "schedule_type": schedule["schedule_type"],
            "schedule_interval_seconds": schedule["schedule_interval_seconds"],
            "schedule_daily_time": schedule["schedule_daily_time"],
        },
    )


@app.get("/admin/found", response_class=HTMLResponse)
async def admin_found(request: Request, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(FoundProduct).order_by(FoundProduct.created_at.desc()).limit(500))
    products = r.scalars().all()
    return templates.TemplateResponse("found.html", {"request": request, "products": products})


@app.post("/admin/tasks/create", response_class=RedirectResponse)
async def admin_task_create(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    brand = (form.get("brand") or "").strip()
    model = (form.get("model") or "").strip()
    min_price_raw = (form.get("min_price") or "").strip()
    if not brand or not model or not min_price_raw:
        return RedirectResponse(url="/?tab=tasks&error=1", status_code=303)
    try:
        min_price = float(min_price_raw.replace(",", "."))
    except ValueError:
        return RedirectResponse(url="/?tab=tasks&error=1", status_code=303)
    is_active = form.get("is_active") == "on"
    run_now = form.get("run_now") == "on"

    task = SearchTask(brand=brand, model=model, min_price=min_price, is_active=is_active)
    db.add(task)
    await db.flush()
    task_id = task.id
    await db.commit()
    if run_now:
        background_tasks.add_task(run_search_task, task_id)
    return RedirectResponse(url="/?tab=tasks&created=1", status_code=303)


@app.post("/admin/settings/use-proxy", response_class=RedirectResponse)
async def admin_set_use_proxy(request: Request):
    use_proxy = request.query_params.get("use_proxy") == "1"
    async with async_session() as db:
        r = await db.execute(select(Setting).where(Setting.key == "use_proxy"))
        row = r.scalar_one_or_none()
        if not row:
            db.add(Setting(key="use_proxy", value="true" if use_proxy else "false"))
        else:
            row.value = "true" if use_proxy else "false"
        await db.commit()
    set_use_proxy_global(use_proxy)
    return RedirectResponse(url="/?tab=tasks&proxy_updated=1", status_code=303)


@app.post("/admin/settings/schedule", response_class=RedirectResponse)
async def admin_set_schedule(request: Request):
    form = await request.form()
    schedule_type = (form.get("schedule_type") or "").strip() or ""
    schedule_interval_seconds = (form.get("schedule_interval_seconds") or "").strip()
    schedule_daily_time = (form.get("schedule_daily_time") or "").strip()
    async with async_session() as db:
        for key, value in (
            ("schedule_type", schedule_type),
            ("schedule_interval_seconds", schedule_interval_seconds),
            ("schedule_daily_time", schedule_daily_time),
        ):
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            if not row:
                db.add(Setting(key=key, value=value))
            else:
                row.value = value
        await db.commit()
    await refresh_scheduler()
    return RedirectResponse(url="/?tab=tasks&schedule_saved=1", status_code=303)


@app.post("/admin/tasks/{task_id}/run", response_class=RedirectResponse)
async def admin_task_run(task_id: int, background_tasks: BackgroundTasks):
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        task = r.scalar_one_or_none()
    if not task:
        return RedirectResponse(url="/?tab=tasks", status_code=303)
    if task.run_status == "running":
        return RedirectResponse(url="/?tab=tasks&running=1", status_code=303)
    background_tasks.add_task(run_search_task, task_id)
    return RedirectResponse(url="/?tab=tasks&run=1", status_code=303)


@app.post("/admin/tasks/run-all", response_class=RedirectResponse)
async def admin_run_all(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.is_active == True))
    tasks = [t for t in r.scalars().all() if t.run_status != "running"]
    for t in tasks:
        background_tasks.add_task(run_search_task, t.id)
    return RedirectResponse(url="/?tab=tasks&run_all=1", status_code=303)


@app.post("/admin/tasks/{task_id}/stop", response_class=RedirectResponse)
async def admin_task_stop(task_id: int):
    request_cancel(task_id)
    return RedirectResponse(url="/?tab=tasks&stop=1", status_code=303)


@app.post("/admin/tasks/{task_id}/toggle-active", response_class=RedirectResponse)
async def admin_task_toggle(task_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if task:
        task.is_active = not task.is_active
        await db.commit()
        await refresh_scheduler()
    return RedirectResponse(url="/?tab=tasks", status_code=303)


@app.post("/admin/tasks/{task_id}/delete", response_class=RedirectResponse)
async def admin_task_delete(task_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if task:
        await db.delete(task)
        await db.commit()
        await refresh_scheduler()
    return RedirectResponse(url="/?tab=tasks", status_code=303)


@app.post("/admin/proxies", response_class=RedirectResponse)
async def admin_proxies_save(request: Request):
    form = await request.form()
    text = (form.get("proxy_list") or "").strip()
    urls = [u.strip() for u in text.replace(",", "\n").splitlines() if u.strip()]
    async with async_session() as db:
        await db.execute(delete(Proxy))
        for u in urls:
            db.add(Proxy(url=u))
        await db.commit()
    await refresh_proxy_list()
    return RedirectResponse(url="/?tab=proxies&saved=1", status_code=303)
