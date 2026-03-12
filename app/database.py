"""Подключение к БД и сессии."""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import select, text
from app.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


DEFAULT_BRANDS = [
    ("Apple", "26303000"),
    ("Samsung", "24565087"),
    ("Xiaomi", "32686750"),
    ("Google", "76075458"),
    ("Huawei", "26303185"),
    ("Realme", "158297487"),
    ("Honor", "142019112"),
    ("Poco", "87259178"),
]


async def _seed_brands_if_empty():
    from app.models import Brand
    async with async_session() as db:
        r = await db.execute(select(Brand).limit(1))
        if r.scalar_one_or_none():
            return
        for name, code in DEFAULT_BRANDS:
            db.add(Brand(name=name.strip(), code=str(code).strip()))
        await db.commit()


async def _seed_use_proxy_setting():
    from app.models import Setting
    async with async_session() as db:
        r = await db.execute(select(Setting).where(Setting.key == "use_proxy"))
        if r.scalar_one_or_none():
            return
        db.add(Setting(key="use_proxy", value="false"))
        await db.commit()


async def init_db():
    from app.models import Brand, SearchTask, FoundProduct, Setting, Proxy
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Добавляем колонку url в search_tasks, если её ещё нет (для перехода от бренда/модели к произвольному URL).
        try:
            res = await conn.execute(text("PRAGMA table_info(search_tasks)"))
            cols = [row[1] for row in res]  # row[1] — имя колонки в PRAGMA table_info
            if "url" not in cols:
                await conn.execute(text("ALTER TABLE search_tasks ADD COLUMN url VARCHAR(1024)"))
        except Exception:
            # Если по какой-то причине не удалось, не блокируем запуск приложения.
            pass
    await _seed_brands_if_empty()
    await _seed_use_proxy_setting()
