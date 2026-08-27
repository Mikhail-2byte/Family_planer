"""Настройка соединения SQLite и остановка фоновой задачи — этап 2.0."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db import sqlite  # noqa: F401 — вешает слушатель на класс Engine
from bot.services import ticker


@pytest.mark.asyncio
async def test_file_database_runs_in_wal(tmp_path):
    """Без WAL тикер и хендлер начнут ловить `database is locked`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}")
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        assert mode == "wal"
        assert timeout == sqlite.BUSY_TIMEOUT_MS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_database_is_untouched():
    """На базе в памяти WAL не применяется — старые тесты не должны сломаться."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        assert mode == "memory"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ticker_stops_on_cancel(monkeypatch, bot, session_maker):
    """`run` не должен глотать CancelledError, иначе остановка бота повиснет."""
    monkeypatch.setattr(ticker, "Session", session_maker)

    task = asyncio.create_task(ticker.run(bot))
    await asyncio.sleep(0)  # дать циклу дойти до первого await
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_tick_failure_does_not_kill_the_loop(monkeypatch, bot, session_maker):
    calls = {"n": 0}

    async def boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("что-то отвалилось")

    monkeypatch.setattr(ticker, "Session", session_maker)
    monkeypatch.setattr(ticker, "tick_once", boom)
    monkeypatch.setattr(ticker.settings, "tick_seconds", 0)

    task = asyncio.create_task(ticker.run(bot))
    while calls["n"] < 3:  # цикл пережил два падения и крутится дальше
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
