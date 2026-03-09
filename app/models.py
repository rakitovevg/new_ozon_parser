"""Модели БД."""
from datetime import datetime
from sqlalchemy import String, Text, Float, Integer, Boolean, DateTime, Column, ForeignKey
from app.database import Base


class Brand(Base):
    """Бренды: name — для отображения в выпадающем списке, code — для подстановки в URL."""
    __tablename__ = "brands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    code = Column(String(128), nullable=False)


class SearchTask(Base):
    """Задача поиска: бренд (name из Brand), модель, мин. цена; расписание; без своего use_proxy — используется глобальный."""
    __tablename__ = "search_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(128), nullable=False)   # Brand.name
    model = Column(String(256), nullable=False)
    min_price = Column(Float, nullable=False)     # уведомить, если цена в карточке <= min_price
    is_active = Column(Boolean, default=True)
    schedule_type = Column(String(32), nullable=True)   # 'interval' | 'daily'
    schedule_interval_seconds = Column(Integer, nullable=True)
    schedule_daily_time = Column(String(8), nullable=True)  # "HH:MM"
    run_status = Column(String(32), default="idle")  # idle | running | completed | failed | cancelled
    run_error = Column(Text, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FoundProduct(Base):
    """Найденный товар (цена не выше порога задачи)."""
    __tablename__ = "found_products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("search_tasks.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(512), nullable=False)
    price = Column(Float, nullable=False)
    link = Column(String(1024), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    """Ключ-значение настроек (use_proxy, proxy_list — опционально)."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False)
    value = Column(Text, nullable=True)


class Proxy(Base):
    """Прокси из загруженного списка (одна строка — один URL)."""
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
