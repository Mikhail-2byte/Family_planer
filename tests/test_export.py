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
async def test_export_sends_all_three_files(session, family, anya):
    """Календарь третьим — у записи из `_entry` есть срок, значит он не пуст."""
    await _entry(session, family, anya)
    message = FakeMessage()

    await cmd_export(message, session, family)

    stem = f"family-{tu.local_today(family.tz).isoformat()}"
    names = [document.filename for document, _ in message.documents]
    assert names == [f"{stem}.md", f"{stem}.csv", f"{stem}.ics"]


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


# --- Календарь (.ics) ---------------------------------------------------------
#
# Односторонняя выгрузка вместо отменённой интеграции с Google Календарём:
# события видны в любом календаре, а OAuth и `token.json` не нужны.


def _events(payload: bytes) -> list[dict[str, str]]:
    """Разобрать .ics в список событий. Наивно, но ровно под наш же формат."""
    text = payload.decode("utf-8")
    # Снимаем перенос длинных строк, прежде чем читать: продолжение начинается
    # с пробела — так же, как это делают настоящие календари
    unfolded = text.replace("\r\n ", "")
    events, current = [], None
    for line in unfolded.split("\r\n"):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value
    return events


@pytest.mark.asyncio
async def test_ics_has_a_valid_envelope(session, family, anya):
    await _entry(session, family, anya)
    entries = await repo.all_entries(session, family.id)

    payload = export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    text = payload.decode("utf-8")

    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "VERSION:2.0" in text
    # CRLF — требование RFC, и не формальное: часть календарей на голом \n
    # молча показывает пустой файл
    assert "\n" not in text.replace("\r\n", "")


@pytest.mark.asyncio
async def test_ics_keeps_time_and_all_day_apart(session, family, anya):
    """Запись «на весь день» обязана стать полосой на день, а не встречей в полночь."""
    await _entry(session, family, anya, title="Встреча")
    await _entry(
        session,
        family,
        anya,
        title="Отпуск",
        all_day=True,
        due_at=datetime(2026, 9, 1, 0, 0),
    )
    entries = await repo.all_entries(session, family.id)

    events = _events(
        export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    )
    by_title = {e["SUMMARY"]: e for e in events}

    assert by_title["Встреча"]["DTSTART"] == "20260828T160000Z"
    assert by_title["Отпуск"]["DTSTART;VALUE=DATE"] == "20260901"


@pytest.mark.asyncio
async def test_ics_skips_entries_without_a_due_date(session, family, anya):
    """Заметке без срока в календаре места нет, а пустая дата ломает файл."""
    await _entry(session, family, anya, kind="note", title="Без срока", due_at=None)
    await _entry(session, family, anya, title="Со сроком")
    entries = await repo.all_entries(session, family.id)

    events = _events(
        export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    )
    assert [e["SUMMARY"] for e in events] == ["Со сроком"]


@pytest.mark.asyncio
async def test_ics_omits_deleted_entries(session, family, anya):
    """В отличие от CSV и Markdown: те — слепок базы, календарь — инструмент."""
    entry = await _entry(session, family, anya, title="Выброшено")
    await repo.archive_entry(session, entry.id, family.id)
    entries = await repo.all_entries(session, family.id)

    events = _events(
        export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    )
    assert events == []


@pytest.mark.asyncio
async def test_ics_escapes_special_characters(session, family, anya):
    r"""Запятая и `;` в заголовке — разделители полей RFC 5545.

    Порядок экранирования важен: обратный слэш обязан идти первым, иначе слэш,
    добавленный к запятой, экранируется следующим проходом.
    """
    await _entry(session, family, anya, title=r"Купить: молоко, хлеб; и 100% сок \ да")
    entries = await repo.all_entries(session, family.id)

    events = _events(
        export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    )
    assert events[0]["SUMMARY"] == "Купить: молоко\\, хлеб\\; и 100% сок \\\\ да"


@pytest.mark.asyncio
async def test_ics_folds_long_lines(session, family, anya):
    """75 октетов на строку — лимит RFC. Кириллица занимает по два байта."""
    await _entry(session, family, anya, title="я" * 300)
    entries = await repo.all_entries(session, family.id)

    payload = export.to_ics(entries, family.tz, "Семья", datetime(2026, 8, 29, 6, 0))
    for line in payload.decode("utf-8").split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line[:40]


@pytest.mark.asyncio
async def test_ics_uids_are_stable_across_exports(session, family, anya):
    """Иначе повторная выгрузка заводит дубли вместо обновления записей."""
    await _entry(session, family, anya)
    entries = await repo.all_entries(session, family.id)

    first = _events(export.to_ics(entries, family.tz, "С", datetime(2026, 8, 29, 6, 0)))
    second = _events(export.to_ics(entries, family.tz, "С", datetime(2026, 9, 1, 6, 0)))

    assert first[0]["UID"] == second[0]["UID"]
    assert first[0]["UID"].endswith(export.ICS_DOMAIN)


@pytest.mark.asyncio
async def test_export_sends_the_calendar_as_a_third_file(session, family, anya):
    await _entry(session, family, anya)
    message = FakeMessage()

    await cmd_export(message, session, family)

    names = [doc.filename for doc, _ in message.documents]
    assert [n.rsplit(".", 1)[-1] for n in names] == ["md", "csv", "ics"]


@pytest.mark.asyncio
async def test_export_skips_an_empty_calendar(session, family, anya):
    """Валидный, но пустой .ics говорит человеку только «что-то сломалось»."""
    await _entry(session, family, anya, kind="note", title="Мысль", due_at=None)
    message = FakeMessage()

    await cmd_export(message, session, family)

    names = [doc.filename for doc, _ in message.documents]
    assert not any(n.endswith(".ics") for n in names)
