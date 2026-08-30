"""Запросы к записям и рендер строк — этап 1.3 и 1.5."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from bot import texts
from bot.db import repo
from bot.services import timeutil as tu
from tests.conftest import FakeMessage

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # 12:00 по Москве, четверг


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


@pytest.mark.asyncio
async def test_entries_for_range_takes_only_the_local_day(session, family, anya):
    async def add(title, due):
        return await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="event",
            title=title,
            due_at=due,
        )

    await add("вчера поздно", _msk(2026, 8, 26, 23, 30))
    await add("сегодня утром", _msk(2026, 8, 27, 8, 0))
    await add("сегодня поздно", _msk(2026, 8, 27, 23, 30))
    await add("завтра рано", _msk(2026, 8, 28, 0, 30))

    start, end = tu.day_bounds(tu.local_today(MSK, NOW), MSK)
    found = await repo.entries_for_range(session, family.id, start, end)
    assert [e.title for e in found] == ["сегодня утром", "сегодня поздно"]


@pytest.mark.asyncio
async def test_overdue_only_counts_open_entries(session, family, anya):
    old = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="просрочено",
        due_at=_msk(2026, 8, 20, 10, 0),
    )
    await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="будущее",
        due_at=_msk(2026, 9, 20, 10, 0),
    )
    assert [e.title for e in await repo.overdue_entries(session, family.id, NOW)] == [
        "просрочено"
    ]

    await repo.complete_entry(session, old.id, family.id, anya.id)
    assert await repo.overdue_entries(session, family.id, NOW) == []


@pytest.mark.asyncio
async def test_entries_by_kind_paginates_and_counts(session, family, anya):
    for i in range(10):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="task",
            title=f"задача {i}",
            due_at=_msk(2026, 9, 1, 10, 0) + timedelta(days=i),
        )

    page, total = await repo.entries_by_kind(session, family.id, "task", limit=4)
    assert total == 10
    assert [e.title for e in page] == [f"задача {i}" for i in range(4)]

    page2, _ = await repo.entries_by_kind(session, family.id, "task", limit=4, offset=8)
    assert len(page2) == 2


@pytest.mark.asyncio
async def test_dated_entries_come_before_undated(session, family, anya):
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="без срока"
    )
    await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="со сроком",
        due_at=_msk(2026, 9, 1, 10, 0),
    )
    page, _ = await repo.entries_by_kind(session, family.id, "task")
    assert [e.title for e in page] == ["со сроком", "без срока"]


@pytest.mark.asyncio
async def test_search_ignores_case_and_looks_into_body(session, family, anya):
    await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="note",
        title="Отпуск",
        body="Посмотреть билеты в Сочи",
    )
    assert len(await repo.search_entries(session, family.id, "ОТПУСК")) == 1
    assert len(await repo.search_entries(session, family.id, "сочи")) == 1
    assert await repo.search_entries(session, family.id, "молоко") == []


@pytest.mark.asyncio
async def test_complete_entry_records_who_and_when(session, family, anya):
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="Купить хлеб"
    )
    done = await repo.complete_entry(session, entry.id, family.id, anya.id)
    assert done.status == "done" and done.done_by == anya.id and done.done_at

    # Повторное закрытие ничего не делает
    assert await repo.complete_entry(session, entry.id, family.id, anya.id) is None


@pytest.mark.asyncio
async def test_complete_entry_rejects_foreign_family(session, family, anya):
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="Своё"
    )
    assert await repo.complete_entry(session, entry.id, family.id + 1, anya.id) is None


@pytest.mark.asyncio
async def test_entry_counts_by_author(session, family, anya):
    misha = await repo.get_or_create_member(session, family.id, 111, "Миша")
    for author in (anya, anya, misha):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=author.id,
            kind="note",
            title="что-то",
        )
    counts = await repo.entry_counts_by_author(session, family.id)
    assert counts == {anya.id: 2, misha.id: 1}


@pytest.mark.asyncio
async def test_entry_line_shows_author_and_time(session, family, anya):
    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="Купить молоко",
        due_at=_msk(2026, 8, 28, 19, 0),
    )
    await session.refresh(entry, ["author"])
    line = texts.entry_line(entry, MSK, NOW)
    assert "Купить молоко" in line
    assert "завтра, 28 авг, 19:00" in line
    assert "Аня" in line


@pytest.mark.asyncio
async def test_entry_line_escapes_html(session, family, anya):
    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="note",
        title="<b>жирный</b> & хитрый",
    )
    await session.refresh(entry, ["author"])
    line = texts.entry_line(entry, MSK, NOW)
    assert "&lt;b&gt;жирный&lt;/b&gt; &amp; хитрый" in line


@pytest.mark.asyncio
async def test_done_entry_is_struck_through(session, family, anya):
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="Хлеб"
    )
    await repo.complete_entry(session, entry.id, family.id, anya.id)
    await session.refresh(entry, ["author"])
    assert "<s>Хлеб</s>" in texts.entry_line(entry, MSK, NOW)


@pytest.mark.asyncio
async def test_all_day_entry_hides_time(session, family, anya):
    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="event",
        title="День рождения",
        due_at=_msk(2026, 8, 28),
        all_day=True,
    )
    await session.refresh(entry, ["author"])
    card = texts.entry_card(entry, MSK, NOW)
    assert "завтра, 28 авг" in card and "00:00" not in card


# --- ссылка на исходное сообщение, шаг 3a.7 -----------------------------------


async def _sourced(session, family, anya, chat_id, message_id):
    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="Купить молоко",
        source_chat_id=chat_id,
        source_message_id=message_id,
    )
    await session.refresh(entry, ["author"])
    return texts.entry_card(entry, MSK, NOW)


@pytest.mark.asyncio
async def test_card_links_to_the_source_message(session, family, anya):
    card = await _sourced(session, family, anya, -1001234567890, 42)
    assert 'href="https://t.me/c/1234567890/42"' in card


@pytest.mark.asyncio
async def test_plain_group_gets_no_link(session, family, anya):
    """У обычной группы (chat_id без -100) ссылок на сообщения не бывает вовсе."""
    assert "t.me" not in await _sourced(session, family, anya, -5001, 42)


@pytest.mark.asyncio
async def test_entry_without_source_message_gets_no_link(session, family, anya):
    """Записи мастера `/new`: chat_id он пишет, а message_id — нет."""
    assert "t.me" not in await _sourced(session, family, anya, -1001234567890, None)


@pytest.mark.asyncio
async def test_render_page_numbers_entries_and_builds_nav(session, family, anya):
    from bot.handlers.views import _render_page

    for i in range(10):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="task",
            title=f"задача {i}",
        )

    text, markup = await _render_page(session, family, "tasks", 0)
    assert "1. " in text and "8. " in text and "9. " not in text
    assert "1–8" in text and "10" in text

    # Ряд на запись: закрыть и открыть карточку. Последний ряд — навигация
    *entry_rows, nav_row = markup.inline_keyboard
    assert [[b.text for b in row] for row in entry_rows] == [
        [f"✅ {i}", f"✏️ {i}"] for i in range(1, 9)
    ]
    assert [b.text for b in nav_row] == ["→"]  # назад некуда

    _, markup2 = await _render_page(session, family, "tasks", 8)
    assert [b.text for b in markup2.inline_keyboard[-1]] == ["←"]  # вперёд некуда


@pytest.mark.asyncio
async def test_render_page_on_empty_list_has_no_keyboard(session, family):
    from bot.handlers.views import _render_page

    text, markup = await _render_page(session, family, "tasks", 0)
    assert text == texts.EMPTY_TASKS
    assert markup is None


# --- Правки перед живым прогоном: клавиатура и потолок недели ----------------


@pytest.mark.asyncio
async def test_empty_task_list_falls_back_to_the_main_keyboard(session, family, anya):
    """Inline и нижняя клавиатура в одном сообщении несовместимы, поэтому
    нижняя достаётся только пустому списку — где экран иначе остаётся вообще
    без кнопок и читается как «бот сломался»."""
    from aiogram.types import InlineKeyboardMarkup

    from bot import keyboards as kb
    from bot.handlers import views

    empty = FakeMessage()
    await views.cmd_tasks(empty, session, family)
    assert empty.texts == [texts.EMPTY_TASKS]
    assert empty.replies[0][1]["reply_markup"] == kb.main_keyboard()

    await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="Купить хлеб",
    )
    filled = FakeMessage()
    await views.cmd_tasks(filled, session, family)
    assert isinstance(filled.replies[0][1]["reply_markup"], InlineKeyboardMarkup)


def _this_week():
    """Текущая неделя — та же, что возьмёт хендлер: он зовёт `now_utc()` сам."""
    today = tu.local_today(MSK)
    monday = today - timedelta(days=today.weekday())
    start, _ = tu.week_bounds(today, MSK)
    return monday, start


@pytest.mark.asyncio
async def test_week_is_capped_and_carries_the_keyboard(session, family, anya):
    """Без потолка семь дней перерастают 4096 символов и /week отваливается
    целиком с TelegramBadRequest."""
    from bot import keyboards as kb
    from bot.handlers import views

    _, start = _this_week()
    for i in range(60):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="event",
            title=f"встреча номер {i} по поводу молока и садика",
            due_at=start + timedelta(hours=i * 2),  # 118 ч < 168 ч недели
        )

    message = FakeMessage()
    await views.cmd_week(message, session, family)

    text, kwargs = message.replies[0]
    assert len(text) < 4096
    assert texts.MORE_ITEMS.format(count=60 - texts.MAX_WEEK_ITEMS) in text
    assert kwargs["reply_markup"] == kb.main_keyboard()


@pytest.mark.asyncio
async def test_week_cut_drops_the_tail_not_the_middle(session, family, anya):
    """Выборка отсортирована `all_day DESC, due_at`, поэтому срез сырого списка
    оставил бы все «весь день» за неделю и обрезал бы ближайшие дни."""
    from bot.handlers import views

    monday, start = _this_week()
    for i in range(20):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="event",
            title=f"вс {i}",
            due_at=tu.to_utc(datetime.combine(monday + timedelta(days=6), time()), MSK),
            all_day=True,
        )
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="event",
            title=f"пн {i}",
            due_at=start + timedelta(minutes=i),
        )

    message = FakeMessage()
    await views.cmd_week(message, session, family)

    text = message.texts[0]
    # Потолок 30: понедельник помещается целиком, в хвост уходит воскресенье
    assert text.count("пн ") == 20
    assert text.count("вс ") == 10
    assert texts.MORE_ITEMS.format(count=10) in text


# --- Закрытие заметок ---------------------------------------------------------
#
# До этой правки закрыть можно было только задачу: кнопка «Готово» рисовалась
# при `view == "tasks"`, а `/notes` вдобавок показывал записи любого статуса.
# Заметки копились вечно, а заметка с прошедшим сроком навсегда оседала в блоке
# «Просрочено» утренней сводки — убрать её можно было только руками в базе.


class _DoneCall:
    """Колбэк с `edit_text`: `FakeMessage` из conftest его не умеет."""

    def __init__(self, chat_id: int):
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.message = SimpleNamespace(
            message_id=500,
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            edit_text=self._edit,
        )

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)

    async def _edit(self, text: str, reply_markup=None) -> None:
        self.edits.append(text)


@pytest.mark.asyncio
async def test_notes_page_offers_a_close_button(session, family, anya):
    from bot.handlers.views import _render_page

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note", title="идея"
    )

    _, markup = await _render_page(session, family, "notes", 0)
    # Подпись своя: заметку не «выполняют», её убирают с глаз (этап 10).
    # Колбэк при этом прежний `DoneCB` — механизм у обеих один
    assert [b.text for b in markup.inline_keyboard[0]] == ["🗄 1", "✏️ 1"]


@pytest.mark.asyncio
async def test_closed_entry_leaves_the_list_and_the_overdue_block(
    session, family, anya, bot
):
    """Закрытая запись с прошедшим сроком висела в «Просрочено» каждое утро вечно.

    Баг был про механизм закрытия, а не про заметки, поэтому проверяется он на
    задаче. Заметку сюда больше не подставить: с этапа 10 она в «Просрочено» не
    попадает вовсе — это отдельная история, и стережёт её
    `test_regressions_live.test_note_with_a_past_due_is_never_overdue`.
    """
    from bot import keyboards as kb
    from bot.handlers.views import _render_page, mark_done
    from bot.services import digest

    task = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="Полить цветы",
        due_at=_msk(2026, 8, 27, 7),
    )
    now = _msk(2026, 8, 28, 9)
    body, _ = await digest.build_day(session, family, now)
    assert "Полить цветы" in body

    call = _DoneCall(family.chat_id)
    await mark_done(
        call, kb.DoneCB(entry_id=task.id, offset=0), session, family, anya, bot
    )

    assert (await repo.get_entry(session, task.id)).status == "done"
    body, _ = await digest.build_day(session, family, now)
    assert "Полить цветы" not in body
    text, markup = await _render_page(session, family, "tasks", 0)
    assert text == texts.EMPTY_TASKS and markup is None


@pytest.mark.asyncio
async def test_closing_a_note_redraws_notes_not_tasks(session, family, anya, bot):
    """`mark_done` перерисовывал страницу жёстко как «tasks».

    Вид приходится брать из типа записи: поле в `DoneCB` завести нельзя —
    у кнопок, уже висящих в чате, callback_data вида `done:42:0`, и лишнее поле
    сделало бы их неразбираемыми.
    """
    from bot import keyboards as kb
    from bot.handlers.views import mark_done

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="ЗАДАЧА"
    )
    note = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note", title="заметка"
    )
    # Третья запись нужна тем, что она есть: без неё страница после закрытия
    # заметки оказалась бы пустой, и проверять было бы нечего
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note", title="вторая"
    )

    call = _DoneCall(family.chat_id)
    await mark_done(
        call, kb.DoneCB(entry_id=note.id, offset=0), session, family, anya, bot
    )

    assert call.answers == [texts.NOTE_CLOSED.format(title="заметка")]
    assert "вторая" in call.edits[0]
    assert "ЗАДАЧА" not in call.edits[0]
