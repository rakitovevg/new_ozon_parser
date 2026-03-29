from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.models import FoundProduct, Proxy, SearchTask
from app.proxy_rotation import refresh_proxy_list
from app.scheduler import refresh_scheduler, request_cancel, run_all_active_tasks, run_search_task
from app.services.settings_service import (
    get_cached_use_proxy,
    get_schedule_settings,
    set_schedule_settings,
    set_use_proxy,
    sync_use_proxy_from_db,
)

router = APIRouter(tags=["admin"])


def _tasks_tab_url(*, filter_brand: str | None = None, **query: str) -> str:
    q: dict[str, str] = {"tab": "tasks"}
    for k, v in query.items():
        if v:
            q[k] = v
    if filter_brand:
        q["filter_brand"] = filter_brand
    return "/?" + urlencode(q)


def _filter_brand_from_form(form) -> str | None:
    v = (form.get("filter_brand") or "").strip().lower()
    return v or None


@router.get("/", response_class=HTMLResponse)
async def admin_index(request: Request, db: AsyncSession = Depends(get_db)):
    await sync_use_proxy_from_db()
    filter_brand = (request.query_params.get("filter_brand") or "").strip().lower()

    stmt = select(SearchTask).order_by(func.lower(SearchTask.brand).asc(), SearchTask.created_at.desc())
    if filter_brand:
        stmt = stmt.where(func.lower(SearchTask.brand) == filter_brand)
    r = await db.execute(stmt)
    tasks = r.scalars().all()

    r_all = await db.execute(select(SearchTask))
    all_for_brands = r_all.scalars().all()
    seen_brand_lower: set[str] = set()
    filter_brand_options: list[str] = []
    for t in all_for_brands:
        raw = (t.brand or "").strip()
        if not raw:
            continue
        key = raw.lower()
        if key not in seen_brand_lower:
            seen_brand_lower.add(key)
            filter_brand_options.append(key)
    filter_brand_options.sort()

    active_brands_lower: list[str] = []
    seen_active: set[str] = set()
    for t in all_for_brands:
        if not t.is_active:
            continue
        raw = (t.brand or "").strip()
        if not raw:
            continue
        key = raw.lower()
        if key not in seen_active:
            seen_active.add(key)
            active_brands_lower.append(key)
    active_brands_lower.sort()
    r3 = await db.execute(select(Proxy).order_by(Proxy.id))
    proxies = r3.scalars().all()
    r4 = await db.execute(select(FoundProduct).order_by(FoundProduct.created_at.desc()).limit(50))
    found_products = r4.scalars().all()
    schedule = await get_schedule_settings()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tasks": tasks,
            "brands": [],
            "use_proxy": get_cached_use_proxy(),
            "proxies": proxies,
            "found_products": found_products,
            "schedule_type": schedule["schedule_type"],
            "schedule_interval_seconds": schedule["schedule_interval_seconds"],
            "schedule_daily_time": schedule["schedule_daily_time"],
            "active_brands_lower": active_brands_lower,
            "filter_brand": filter_brand,
            "filter_brand_options": filter_brand_options,
        },
    )


@router.get("/admin/found", response_class=HTMLResponse)
async def admin_found(request: Request, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(FoundProduct).order_by(FoundProduct.created_at.desc()).limit(500))
    products = r.scalars().all()
    templates = request.app.state.templates
    return templates.TemplateResponse("found.html", {"request": request, "products": products})


@router.post("/admin/tasks/create", response_class=RedirectResponse)
async def admin_task_create(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    url = (form.get("url") or "").strip()
    brand = (form.get("brand") or "").strip()
    model = (form.get("model") or "").strip()
    min_price_raw = (form.get("min_price") or "").strip()
    if not url or not min_price_raw:
        return RedirectResponse(url=_tasks_tab_url(error="1", filter_brand=fb), status_code=303)
    try:
        min_price = float(min_price_raw.replace(",", "."))
    except ValueError:
        return RedirectResponse(url=_tasks_tab_url(error="1", filter_brand=fb), status_code=303)
    is_active = form.get("is_active") == "on"
    run_now = form.get("run_now") == "on"

    task = SearchTask(brand=brand, model=model, url=url, min_price=min_price, is_active=is_active)
    db.add(task)
    await db.flush()
    task_id = task.id
    await db.commit()
    if run_now:
        background_tasks.add_task(run_search_task, task_id)
    return RedirectResponse(url=_tasks_tab_url(created="1", filter_brand=fb), status_code=303)


@router.post("/admin/settings/use-proxy", response_class=RedirectResponse)
async def admin_set_use_proxy(request: Request):
    use_proxy = request.query_params.get("use_proxy") == "1"
    await set_use_proxy(use_proxy)
    return RedirectResponse(url="/?tab=tasks&proxy_updated=1", status_code=303)


@router.post("/admin/settings/schedule", response_class=RedirectResponse)
async def admin_set_schedule(request: Request):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    await set_schedule_settings(
        schedule_type=(form.get("schedule_type") or "").strip(),
        schedule_interval_seconds=(form.get("schedule_interval_seconds") or "").strip(),
        schedule_daily_time=(form.get("schedule_daily_time") or "").strip(),
    )
    await refresh_scheduler()
    return RedirectResponse(url=_tasks_tab_url(schedule_saved="1", filter_brand=fb), status_code=303)


@router.post("/admin/tasks/{task_id}/run", response_class=RedirectResponse)
async def admin_task_run(request: Request, task_id: int, background_tasks: BackgroundTasks):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    async with async_session() as db:
        r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
        task = r.scalar_one_or_none()
    if not task:
        return RedirectResponse(url=_tasks_tab_url(filter_brand=fb), status_code=303)
    if task.run_status == "running":
        return RedirectResponse(url=_tasks_tab_url(running="1", filter_brand=fb), status_code=303)
    background_tasks.add_task(run_search_task, task_id)
    return RedirectResponse(url=_tasks_tab_url(run="1", filter_brand=fb), status_code=303)


@router.post("/admin/tasks/run-all", response_class=RedirectResponse)
async def admin_run_all(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    background_tasks.add_task(run_all_active_tasks)
    return RedirectResponse(url=_tasks_tab_url(run_all="1", filter_brand=fb), status_code=303)


@router.post("/admin/tasks/run-by-brand", response_class=RedirectResponse)
async def admin_run_by_brand(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    brand_key = (form.get("brand") or "").strip().lower()
    if not brand_key:
        return RedirectResponse(url=_tasks_tab_url(filter_brand=fb), status_code=303)
    r = await db.execute(select(SearchTask).where(SearchTask.is_active == True))
    to_run = [
        t
        for t in r.scalars().all()
        if t.run_status != "running" and (t.brand or "").strip().lower() == brand_key
    ]
    if not to_run:
        return RedirectResponse(url=_tasks_tab_url(run_brand_none="1", filter_brand=fb), status_code=303)

    async def _run_brand_queue() -> None:
        for t in to_run:
            await run_search_task(t.id)

    background_tasks.add_task(_run_brand_queue)
    return RedirectResponse(url=_tasks_tab_url(run_brand="1", filter_brand=fb), status_code=303)


@router.post("/admin/tasks/{task_id}/stop", response_class=RedirectResponse)
async def admin_task_stop(request: Request, task_id: int):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    request_cancel(task_id)
    return RedirectResponse(url=_tasks_tab_url(stop="1", filter_brand=fb), status_code=303)


@router.post("/admin/tasks/{task_id}/toggle-active", response_class=RedirectResponse)
async def admin_task_toggle(request: Request, task_id: int, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if task:
        task.is_active = not task.is_active
        await db.commit()
        await refresh_scheduler()
    return RedirectResponse(url=_tasks_tab_url(filter_brand=fb), status_code=303)


@router.post("/admin/tasks/{task_id}/delete", response_class=RedirectResponse)
async def admin_task_delete(request: Request, task_id: int, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    fb = _filter_brand_from_form(form)
    r = await db.execute(select(SearchTask).where(SearchTask.id == task_id))
    task = r.scalar_one_or_none()
    if task:
        await db.delete(task)
        await db.commit()
        await refresh_scheduler()
    return RedirectResponse(url=_tasks_tab_url(filter_brand=fb), status_code=303)


@router.post("/admin/proxies", response_class=RedirectResponse)
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
