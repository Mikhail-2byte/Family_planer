"""Регрессии по багам, найденным ревизией этапов 0–1.

Каждый тест падает на коде «до правки» — это его единственное назначение.
"""

from datetime import date, datetime

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.utils.text_decorations import html_decoration as fmt

from bot import middlewares as mw
from bot import texts
from bot.db import repo
from bot.handlers.new_entry import _parse_day
from bot.handlers.views import PAGE_SIZE, _by_day, _render_page
from bot.services import timeutil as tu

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # четверг, 12:00 по Москве


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


# --- Баг A: миграция группы в супергруппу шла мимо middleware -----------------


def _group_chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="group", title="Семья")


def _update_with(chat: Chat, **message_fields) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1, date=datetime(2026, 8, 27, 12, 0), chat=chat, **message_fields
        ),
    )


@pytest.mark.asyncio
async def test_middleware_moves_family_to_supergroup(session_maker, monkeypatch):
    """Сервисное сообщение о переезде приходит внутри Update, а не как Message.

    Middleware висит на `dp.update`, поэтому проверка `isinstance(event, Message)`
    не срабатывала никогда и ветка миграции была мёртвой.
    """
    monkeypatch.setattr(mw, "Session", session_maker)
    old_chat, new_id = _group_chat(-1001), -1002000

    async with session_maker() as s:
        family = await repo.get_or_create_family(s, old_chat.id, "Семья")

    seen = []

    async def handler(event, data):
        seen.append(event)

    update = _update_with(old_chat, migrate_to_chat_id=new_id)
    result = await mw.FamilyMiddleware()(handler, update, {"event_chat": old_chat})

    # Апдейт проглочен: под старым chat_id семью заводить уже нельзя
    assert result is None and seen == []

    async with session_maker() as s:
        assert await repo.get_family(s, old_chat.id) is None
        moved = await repo.get_family(s, new_id)
        assert moved is not None and moved.id == family.id


@pytest.mark.asyncio
async def test_middleware_registers_family_on_plain_message(session_maker, monkeypatch):
    monkeypatch.setattr(mw, "Session", session_maker)
    chat = _group_chat(-1001)

    got: dict = {}

    async def handler(event, data):
        got.update(data)

    await mw.FamilyMiddleware()(handler, _update_with(chat, text="привет"), {"event_chat": chat})
    assert got["family"].chat_id == chat.id


@pytest.mark.asyncio
async def test_middleware_gives_no_family_in_private(session_maker, monkeypatch):
    """Ровно поэтому у групповых хендлеров обязателен фильтр IN_GROUP."""
    monkeypatch.setattr(mw, "Session", session_maker)
    chat = Chat(id=555, type="private")

    got: dict = {}

    async def handler(event, data):
        got.update(data)

    await mw.FamilyMiddleware()(handler, _update_with(chat, text="привет"), {"event_chat": chat})
    assert "session" in got and "family" not in got


# --- Баг B: /week дублировал дни ---------------------------------------------


@pytest.mark.asyncio
async def test_week_groups_days_in_order_without_repeats(session, family, anya):
    async def add(title, due, all_day=False):
        return await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="event",
            title=title,
            due_at=due,
            all_day=all_day,
        )

    await add("пн 10:00", _msk(2026, 8, 24, 10, 0))
    await add("ср весь день", _msk(2026, 8, 26), all_day=True)
    await add("ср 15:00", _msk(2026, 8, 26, 15, 0))
    await add("пт весь день", _msk(2026, 8, 28), all_day=True)
    await add("пт 12:00", _msk(2026, 8, 28, 12, 0))

    start, end = tu.week_bounds(tu.local_today(MSK, NOW), MSK)
    entries = await repo.entries_for_range(session, family.id, start, end)

    days = _by_day(entries, MSK)
    assert [d for d, _ in days] == [date(2026, 8, 24), date(2026, 8, 26), date(2026, 8, 28)]
    # Внутри дня «весь день» остаётся выше записи со временем
    assert [e.title for e in days[1][1]] == ["ср весь день", "ср 15:00"]


# --- Баг E: «Готово» на последней странице оставляло экран без кнопок ---------


@pytest.mark.asyncio
async def test_last_page_falls_back_when_its_only_entry_is_closed(session, family, anya):
    total = PAGE_SIZE + 1
    for i in range(total):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="task",
            title=f"задача {i}",
        )

    tail, _ = await repo.entries_by_kind(
        session, family.id, "task", limit=PAGE_SIZE, offset=PAGE_SIZE
    )
    await repo.complete_entry(session, tail[0].id, family.id, anya.id)

    text, markup = await _render_page(session, family, "tasks", PAGE_SIZE)
    assert text != texts.EMPTY_TASKS
    assert markup is not None  # с этой страницы есть куда вернуться


# --- Баг C: пользовательский текст не экранировался в HTML --------------------


@pytest.mark.parametrize(
    "render, raw",
    [
        (lambda v: texts.search_header(v, 1), "<b"),
        (lambda v: texts.search_empty(v), "<b"),
        (lambda v: texts.family_member(v, 0), "Ан<я> & Co"),
        (lambda v: texts.family_header(v, "Europe/Moscow", "08:00"), "Дом & Сад"),
        (lambda v: texts.pong(v, 2), "<i>"),
    ],
)
def test_user_text_is_escaped(render, raw):
    """Текст человека попадает в сообщение только в экранированном виде.

    Сравнивать с `raw not in out` нельзя: собственная разметка шаблонов
    (`<b>` в заголовке) — это не пользовательский ввод.
    """
    escaped = fmt.quote(raw)
    assert escaped != raw  # иначе тест ничего не проверяет
    assert escaped in render(raw)


# --- Баг H: метасимволы LIKE в поиске ----------------------------------------


@pytest.mark.asyncio
async def test_search_treats_percent_as_plain_text(session, family, anya):
    async def add(title):
        await repo.create_entry(
            session, family_id=family.id, author_id=anya.id, kind="note", title=title
        )

    await add("Скидка 100% на билеты")
    await add("Купить молоко")

    assert len(await repo.search_entries(session, family.id, "100%")) == 1
    # Голый '%' — искомая подстрока, а не «найти всё»
    assert len(await repo.search_entries(session, family.id, "%")) == 1
    assert await repo.search_entries(session, family.id, "_") == []


# --- Баг I: 29 февраля роняло разбор даты в мастере ---------------------------


def test_parse_day_survives_29_february():
    """2028 високосный, 2029 — нет. Перенос на следующий год бросал ValueError."""
    assert _parse_day("29.02", date(2028, 6, 1)) is None
    assert _parse_day("29.02", date(2028, 1, 1)) == date(2028, 2, 29)


# --- Клавиатура списка: номера кнопок совпадают с номерами строк --------------


@pytest.mark.asyncio
async def test_done_buttons_match_numbered_lines(session, family, anya, monkeypatch):
    import bot.handlers.views as views

    monkeypatch.setattr(views, "PAGE_SIZE", 10)
    for i in range(10):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="task",
            title=f"задача {i}",
        )

    text, markup = await views._render_page(session, family, "tasks", 0)
    assert "10. " in text
    assert [b.text for b in markup.inline_keyboard[0]] == [f"✅ {i}" for i in range(1, 11)]


# --- Мастер: чужой тап получает ответ, а не вечный «часик» -------------------


def test_stale_tap_handler_is_registered_last():
    """Ловушка для тапов, не подошедших ни одному состоянию мастера."""
    from bot.handlers import new_entry

    handlers = [h.callback.__name__ for h in new_entry.router.callback_query.handlers]
    assert handlers[-1] == "stale_tap"
    assert "cancel" in handlers and handlers.index("cancel") < handlers.index("stale_tap")
