"""Выгрузка в Markdown и CSV, `/export` и `/backup` (шаги 6.2–6.3).

Хендлеры зовутся напрямую, как в `test_entries.py`; файлы ловятся фейковым
`answer_document` из `conftest.py`.
"""

import csv
import io
import os
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from bot import texts
from bot.db import repo
from bot.handlers.admin import cmd_backup, cmd_export
from bot.services import backup, export
from bot.services import timeutil as tu
from tests.conftest import FakeMessage


async def _entry(session, family, author, **kwargs):
    fields = {
        "kind": "task",
        "title": "Купить молоко",
        "body": None,
        "due_at": datetime(2026, 8, 28, 16, 0),  # 19:00 в Москве
        "all_day": False,
    }
    fields.update(kwargs)
    return await repo.create_entry(
        session, family_id=family.id, author_id=author.id, **fields
    )


def _rows(blob: bytes) -> list[list[str]]:
    text = blob.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=";"))


@pytest.mark.asyncio
async def test_csv_has_a_header_and_a_row_per_entry(session, family, anya):
    await _entry(session, family, anya)
    await _entry(session, family, anya, title="Позвонить маме")

    rows = _rows(export.to_csv(await repo.all_entries(session, family.id), family.tz))

    assert rows[0] == list(export.COLUMNS)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_csv_starts_with_a_bom(session, family, anya):
    """Без BOM русский Excel читает кириллицу как кракозябры."""
    await _entry(session, family, anya)

    blob = export.to_csv(await repo.all_entries(session, family.id), family.tz)

    assert blob.startswith(b"\xef\xbb\xbf")


@pytest.mark.asyncio
async def test_csv_survives_separators_inside_the_title(session, family, anya):
    """Заголовок с `;`, кавычкой и переводом строки обязан остаться одной ячейкой."""
    await _entry(session, family, anya, title='Купить "молоко"; хлеб\nи сыр')

    rows = _rows(export.to_csv(await repo.all_entries(session, family.id), family.tz))

    assert rows[1][3] == 'Купить "молоко"; хлеб и сыр'
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_due_date_is_rendered_in_family_time(session, family, anya):
    """В базе UTC, в файле — локальное время семьи."""
    family.tz = "Asia/Yekaterinburg"  # +5
    await _entry(session, family, anya)

    rows = _rows(export.to_csv(await repo.all_entries(session, family.id), family.tz))

    assert rows[1][5] == "2026-08-28 21:00"


@pytest.mark.asyncio
async def test_all_day_entry_has_no_time(session, family, anya):
    await _entry(
        session, family, anya, all_day=True, due_at=datetime(2026, 8, 27, 21, 0)
    )

    rows = _rows(export.to_csv(await repo.all_entries(session, family.id), family.tz))

    assert rows[1][5] == "2026-08-28"


@pytest.mark.asyncio
async def test_markdown_marks_closed_entries(session, family, anya):
    open_one = await _entry(session, family, anya, title="Открытая")
    closed = await _entry(session, family, anya, title="Закрытая")
    await repo.complete_entry(session, closed.id, family.id, anya.id)

    text = export.to_markdown(
        await repo.all_entries(session, family.id), family.tz, "Семья", date(2026, 8, 28)
    ).decode("utf-8")

    assert "- [ ] **2026-08-28 19:00** Открытая (Аня)" in text
    assert "- [x] **2026-08-28 19:00** Закрытая (Аня)" in text
    assert open_one.status == "open"


@pytest.mark.asyncio
async def test_markdown_carries_no_html(session, family, anya):
    """Сторож против соблазна позвать `texts.entry_line`: в файле теги — мусор."""
    await _entry(session, family, anya, title="Молоко & хлеб <дёшево>")

    text = export.to_markdown(
        await repo.all_entries(session, family.id), family.tz, "Семья", date(2026, 8, 28)
    ).decode("utf-8")

    assert "<b>" not in text
    assert "&amp;" not in text
    assert "Молоко & хлеб <дёшево>" in text


@pytest.mark.asyncio
async def test_markdown_groups_by_kind(session, family, anya):
    await _entry(session, family, anya, kind="note", title="Пароль от вайфая")
    await _entry(session, family, anya, kind="event", title="Совещание")

    text = export.to_markdown(
        await repo.all_entries(session, family.id), family.tz, "Семья", date(2026, 8, 28)
    ).decode("utf-8")

    assert text.index("## События") < text.index("## Заметки")


@pytest.mark.asyncio
async def test_all_entries_never_leaves_the_family(session, family, anya):
    """Изоляция по семье: чужие записи в выгрузку не попадают."""
    await _entry(session, family, anya)
    other = await repo.get_or_create_family(session, -1002, "Соседи")
    stranger = await repo.get_or_create_member(session, other.id, 333, "Чужой")
    await _entry(session, other, stranger, title="Чужая запись")

    found = await repo.all_entries(session, family.id)

    assert [entry.title for entry in found] == ["Купить молоко"]


@pytest.mark.asyncio
async def test_all_entries_takes_items_without_due_date(session, family, anya):
    """У пунктов списка покупок срока нет — но в выгрузке они обязаны быть."""
    await _entry(session, family, anya, kind="shopping", due_at=None, title="Сметана")

    found = await repo.all_entries(session, family.id)

    assert [entry.title for entry in found] == ["Сметана"]


@pytest.mark.asyncio
async def test_export_sends_two_files(session, family, anya):
    await _entry(session, family, anya)
    message = FakeMessage()

    await cmd_export(message, session, family)

    stem = f"family-{tu.local_today(family.tz).isoformat()}"
    names = [document.filename for document, _ in message.documents]
    assert names == [f"{stem}.md", f"{stem}.csv"]


@pytest.mark.asyncio
async def test_export_on_an_empty_family_sends_nothing(session, family):
    message = FakeMessage()

    await cmd_export(message, session, family)

    assert message.documents == []
    assert message.texts == [texts.EXPORT_EMPTY]


# --- /backup ---


@pytest_asyncio.fixture
async def sandbox(tmp_path, monkeypatch):
    """Своя база на файле: снимок снимается с живого движка, а в тестах он в памяти."""
    source = tmp_path / "family.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    monkeypatch.setattr(backup, "engine", engine)
    yield
    await engine.dispose()


class CatchingMessage(FakeMessage):
    """Читает файл прямо в момент отправки.

    После неё смотреть уже не на что: хендлер сносит временный каталог сразу,
    как отдал файл Telegram, — и именно это здесь заодно и проверяется.
    """

    def __init__(self):
        super().__init__()
        self.blobs: list[bytes] = []

    async def answer_document(self, document, **kwargs):
        self.blobs.append(Path(document.path).read_bytes())
        await super().answer_document(document, **kwargs)


@pytest.mark.asyncio
async def test_backup_sends_a_working_database(sandbox, family, tmp_path):
    message = CatchingMessage()

    await cmd_backup(message, family)

    assert len(message.documents) == 1
    document, kwargs = message.documents[0]
    assert document.filename == f"family-{tu.local_today(family.tz).isoformat()}.db"
    assert kwargs["caption"] == texts.backup_caption(tu.local_today(family.tz))

    # Ушедшие байты — рабочая база, а не обломок
    copy = tmp_path / "sent.db"
    copy.write_bytes(message.blobs[0])
    assert sqlite3.connect(copy).execute("SELECT count(*) FROM entries").fetchone() == (0,)


@pytest.mark.asyncio
async def test_backup_refuses_a_file_too_big_for_telegram(sandbox, family, monkeypatch):
    """Отказ честнее проваленной отправки: она читается как поломка бота."""
    monkeypatch.setattr("bot.handlers.admin.MAX_UPLOAD_BYTES", 1)
    message = FakeMessage()

    await cmd_backup(message, family)

    assert message.documents == []
    assert message.texts and message.texts[0].startswith("База выросла")


@pytest.mark.asyncio
async def test_backup_says_so_when_the_snapshot_fails(family, monkeypatch):
    async def boom(dest):
        raise OSError("диск полон")

    monkeypatch.setattr(backup, "snapshot", boom)
    message = FakeMessage()

    await cmd_backup(message, family)

    assert message.documents == []
    assert message.texts == [texts.BACKUP_FAILED]


@pytest.mark.asyncio
async def test_backup_leaves_no_temporary_files(sandbox, family, monkeypatch):
    """Копия базы не должна оставаться во временном каталоге после отправки."""
    made: list[str] = []
    original = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = original(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    message = FakeMessage()

    await cmd_backup(message, family)

    assert made and not os.path.exists(made[0])


@pytest.mark.asyncio
async def test_export_refuses_a_file_too_big_for_telegram(
    session, family, anya, monkeypatch
):
    """Обрезать выгрузку нельзя, но и молча упереться в лимит она не должна."""
    await _entry(session, family, anya)
    monkeypatch.setattr("bot.handlers.admin.MAX_UPLOAD_BYTES", 1)
    message = FakeMessage()

    await cmd_export(message, session, family)

    assert message.documents == []
    assert message.texts and message.texts[0].startswith("Выгрузка выросла")
