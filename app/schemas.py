from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class UseProxyBody(BaseModel):
    use_proxy: bool


class ScheduleBody(BaseModel):
    schedule_type: Optional[str] = None
    schedule_interval_seconds: Optional[int] = None
    schedule_daily_time: Optional[str] = None


class SearchTaskCreate(BaseModel):
    url: str
    min_price: float
    brand: Optional[str] = None
    model: Optional[str] = None
    is_active: bool = True
    run_now: bool = False


class SearchTaskUpdate(BaseModel):
    url: Optional[str] = None
    min_price: Optional[float] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    is_active: Optional[bool] = None
