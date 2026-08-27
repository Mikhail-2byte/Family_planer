import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import Base


@pytest_asyncio.fixture
async def session_maker():
    """Фабрика сессий на общей БД в памяти.

    Движок для `:memory:` держит StaticPool — одно соединение на всех, поэтому
    разные сессии видят одни и те же данные. Это нужно там, где сессию открывает
    не тест, а сам код (например, `FamilyMiddleware`).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_maker):
    async with session_maker() as s:
        yield s


@pytest_asyncio.fixture
async def family(session):
    fam = await repo.get_or_create_family(session, -1001, "Семья")
    fam.tz = "Europe/Moscow"
    await session.commit()
    return fam


@pytest_asyncio.fixture
async def anya(session, family):
    return await repo.get_or_create_member(session, family.id, 222, "Аня")
