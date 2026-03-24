"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from app.config import BASE_DIR
from app.database import init_db
from app.events import broadcaster
from app.proxy_rotation import refresh_proxy_list
from app.routers.admin import router as admin_router
from app.routers.api import router as api_router
from app.scheduler import refresh_scheduler, scheduler
from app.services.settings_service import sync_use_proxy_from_db

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

# Логи в файл рядом с приложением (в той же папке, где OzonParser или корень проекта)
_log_file = BASE_DIR / "ozon_parser.log"
try:
    _file_handler = logging.FileHandler(_log_file, encoding="utf-8")
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(_file_handler)
except Exception:
    pass

(BASE_DIR / "data").mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await sync_use_proxy_from_db()
    await refresh_proxy_list()
    if not scheduler.running:
        scheduler.start()
    await refresh_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="New Ozon Parser", lifespan=lifespan)

# При запуске из PyInstaller-бандла шаблоны лежат рядом с main.py (внутри бандла)
if getattr(sys, "frozen", False):
    templates_dir = Path(__file__).resolve().parent / "templates"
else:
    templates_dir = BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["getattr"] = getattr
app.state.templates = templates


@app.get("/admin/events")
async def admin_events():
    async def gen():
        async for msg in broadcaster.subscribe():
            yield msg

    return StreamingResponse(gen(), media_type="text/event-stream")

app.include_router(api_router)
app.include_router(admin_router)
