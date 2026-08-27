import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import Base


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def family(session):
    fam = await repo.get_or_create_family(session, -1001, "Семья")
    fam.tz = "Europe/Moscow"
    await session.commit()
    return fam


@pytest_asyncio.fixture
async def anya(session, family):
    return await repo.get_or_create_member(session, family.id, 222, "Аня")
