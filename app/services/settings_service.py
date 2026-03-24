from __future__ import annotations

from sqlalchemy import select

from app.config import get_use_proxy_global, set_use_proxy_global
from app.database import async_session
from app.models import Setting


async def sync_use_proxy_from_db() -> bool:
    """Read use_proxy from DB and refresh in-memory cache."""
    async with async_session() as db:
        r = await db.execute(select(Setting).where(Setting.key == "use_proxy"))
        row = r.scalar_one_or_none()
    if row and row.value:
        value = row.value.strip().lower() in ("1", "true", "yes")
    else:
        value = False
    set_use_proxy_global(value)
    return value


async def set_use_proxy(value: bool) -> bool:
    """Persist use_proxy setting and refresh in-memory cache."""
    async with async_session() as db:
        r = await db.execute(select(Setting).where(Setting.key == "use_proxy"))
        row = r.scalar_one_or_none()
        if not row:
            db.add(Setting(key="use_proxy", value="true" if value else "false"))
        else:
            row.value = "true" if value else "false"
        await db.commit()
    set_use_proxy_global(value)
    return value


async def get_schedule_settings() -> dict[str, str]:
    """Read global scheduler settings from DB."""
    out: dict[str, str] = {}
    async with async_session() as db:
        for key in ("schedule_type", "schedule_interval_seconds", "schedule_daily_time"):
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            out[key] = (row.value or "").strip() if row and row.value else ""
    return out


async def set_schedule_settings(
    *,
    schedule_type: str | None,
    schedule_interval_seconds: int | str | None,
    schedule_daily_time: str | None,
) -> None:
    """Persist global scheduler settings in DB."""
    async with async_session() as db:
        rows = (
            ("schedule_type", (schedule_type or "").strip()),
            (
                "schedule_interval_seconds",
                str(schedule_interval_seconds) if schedule_interval_seconds is not None else "",
            ),
            ("schedule_daily_time", (schedule_daily_time or "").strip()),
        )
        for key, value in rows:
            r = await db.execute(select(Setting).where(Setting.key == key))
            row = r.scalar_one_or_none()
            if not row:
                db.add(Setting(key=key, value=value))
            else:
                row.value = value
        await db.commit()


def get_cached_use_proxy() -> bool:
    return get_use_proxy_global()
