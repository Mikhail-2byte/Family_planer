"""Ежедневный снимок базы и ротация копий (шаг 6.1).

База в остальных тестах живёт в `:memory:`, а `settings.db_path` смотрит на
боевую `data/family.db` — поэтому здесь свой файловый движок и свой каталог в
`tmp_path`, ровно как автофикстура `_parse_log` уводит боевой лог.
"""

import sqlite3
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot.config import settings
from bot.services import backup


@pytest_asyncio.fixture(autouse=True)
async def sandbox(tmp_path, monkeypatch):
    """Свой движок на файле и свой каталог копий. Отметку сбоя чистим за собой."""
    source = tmp_path / "family.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, title TEXT)")
    connection.executemany(
        "INSERT INTO entries (title) VALUES (?)", [("молоко",), ("хлеб",)]
    )
    connection.commit()
    connection.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    monkeypatch.setattr(backup, "engine", engine)
    monkeypatch.setattr(backup, "DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "_failed_on", None)
    monkeypatch.setattr(settings, "backup_keep", 7)
    # Зону закрепляем: день снимка считается в `tz_default`, а он читается из
    # боевого `.env`, и на машине с Екатеринбургом «вечер» уезжал в завтра
    monkeypatch.setattr(settings, "tz_default", "Europe/Moscow")
    yield
    await engine.dispose()


NOON = datetime(2026, 8, 28, 9, 0)  # 12:00 в Europe/Moscow — дефолтном tz_default
TODAY = date(2026, 8, 28)


@pytest.mark.asyncio
async def test_snapshot_is_a_working_database():
    """Копия должна открываться и содержать те же строки, а не быть обломком."""
    dest = backup.DIR / "manual.db"
    await backup.snapshot(dest)

    rows = sqlite3.connect(dest).execute("SELECT title FROM entries").fetchall()
    assert [row[0] for row in rows] == ["молоко", "хлеб"]


@pytest.mark.asyncio
async def test_snapshot_leaves_no_temporary_file():
    """Недописанный снимок не должен пережить успешное завершение."""
    dest = backup.DIR / "manual.db"
    await backup.snapshot(dest)

    assert not (dest.parent / backup.TMP.name).exists()


@pytest.mark.asyncio
async def test_snapshot_overwrites_existing_destination():
    """`VACUUM INTO` в существующий файл падает — спасает запись через временное имя."""
    dest = backup.DIR / "manual.db"
    await backup.snapshot(dest)
    await backup.snapshot(dest)  # второй раз по тому же пути

    assert sqlite3.connect(dest).execute("SELECT count(*) FROM entries").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_daily_creates_one_file_per_day():
    created = await backup.run_daily(NOON)

    assert created == backup.path_for(TODAY)
    assert created.exists()


@pytest.mark.asyncio
async def test_daily_does_nothing_twice_in_the_same_day():
    """Файл дня — сам себе отметка «сегодня уже сделано»."""
    first = await backup.run_daily(NOON)
    stamp = first.stat().st_mtime_ns

    assert await backup.run_daily(NOON.replace(hour=18)) is None
    assert first.stat().st_mtime_ns == stamp
    assert len(list(backup.DIR.glob("family-*.db"))) == 1


@pytest.mark.asyncio
async def test_daily_off_when_keep_is_zero(monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 0)

    assert await backup.run_daily(NOON) is None
    assert not backup.DIR.exists()


@pytest.mark.asyncio
async def test_rotation_keeps_the_freshest(monkeypatch):
    monkeypatch.setattr(settings, "backup_keep", 3)
    backup.DIR.mkdir(parents=True)
    for day in range(20, 28):
        (backup.DIR / f"family-2026-08-{day}.db").write_bytes(b"stale")

    await backup.run_daily(NOON)

    assert sorted(path.name for path in backup.DIR.glob("family-*.db")) == [
        "family-2026-08-26.db",
        "family-2026-08-27.db",
        "family-2026-08-28.db",
    ]


@pytest.mark.asyncio
async def test_rotation_only_touches_its_own_files(monkeypatch):
    """Маска ротации — `family-*.db`; чужой файл в каталоге не наше дело."""
    monkeypatch.setattr(settings, "backup_keep", 1)
    backup.DIR.mkdir(parents=True)
    stray = backup.DIR / "чужой.db"
    stray.write_bytes(b"not ours")

    await backup.run_daily(NOON)

    assert stray.exists()


@pytest.mark.asyncio
async def test_failed_backup_stays_quiet(monkeypatch):
    """Сбой снимка не выходит наружу — тик не должен падать из-за бэкапа."""

    async def boom(dest):
        raise OSError("диск полон")

    monkeypatch.setattr(backup, "snapshot", boom)

    assert await backup.run_daily(NOON) is None


@pytest.mark.asyncio
async def test_failed_backup_does_not_retry_every_tick(monkeypatch):
    """Провал помечает сутки отработанными.

    Без этого файла дня нет, значит «пора делать» — и при полном диске тикер
    полез бы за снимком каждую минуту до полуночи, засыпав лог. Та же болезнь,
    что у панели в 2п (`test_unsendable_panel_does_not_loop_every_tick`).
    """
    calls = []

    async def boom(dest):
        calls.append(dest)
        raise OSError("диск полон")

    monkeypatch.setattr(backup, "snapshot", boom)

    await backup.run_daily(NOON)
    await backup.run_daily(NOON.replace(hour=12))
    await backup.run_daily(NOON.replace(hour=18))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_new_day_tries_again_after_a_failure(monkeypatch):
    """Отметка сбоя держится ровно сутки, а не навсегда."""
    attempts = []

    async def boom(dest):
        attempts.append(dest)
        raise OSError("диск полон")

    monkeypatch.setattr(backup, "snapshot", boom)

    await backup.run_daily(NOON)
    await backup.run_daily(NOON.replace(day=29))

    assert len(attempts) == 2
