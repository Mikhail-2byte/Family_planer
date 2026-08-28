"""Карточка сохранённой записи: правка, срок, удаление (этап 7).

Колбэки зовутся напрямую, как в `test_review.py`; сети нет — `FakeBot` из
`conftest.py` и локальные `FakeCall` / `FakeReply`.
"""

from datetime import date, datetime, time, timedelta

import pytest
import pytest_asyncio

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.handlers import entry as handler
from bot.handlers import views
from bot.services import digest, export, review
from bot.services import timeutil as tu
from types import SimpleNamespace

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # 12:00 по Москве, четверг


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


class FakeCall:
    """Колбэк: `answer` + `message.edit_text`."""

    def __init__(self, chat_id: int, message_id: int = 500):
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, object]] = []
        self.message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            edit_text=self._edit,
        )

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def _edit(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    @property
    def screen(self) -> str:
        return self.edits[-1][0] if self.edits else ""

    @property
    def markup(self):
        return self.edits[-1][1] if self.edits else None

    @property
    def alert(self) -> str:
        return self.answers[-1][0] if self.answers else ""

    @property
    def labels(self) -> list[str]:
        markup = self.markup
        if markup is None:
            return []
        return [b.text for row in markup.inline_keyboard for b in row]


class FakeReply:
    """Ответ реплаем на сообщение с карточкой."""

    def __init__(self, chat_id: int, text: str, card_id: int = 500):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = 900
        self.reply_to_message = SimpleNamespace(message_id=card_id)
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


@pytest.fixture(autouse=True)
def _pending():
    handler._pending.clear()
    yield
    handler._pending.clear()


@pytest_asyncio.fixture
async def task(session, family, anya):
    return await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="позвонить маме",
        due_at=_msk(2026, 8, 28, 19, 0),
    )


async def _tap(
    action, entry_id, session, family, bot, *, view="tasks", offset=0, value=0, call=None
):
    call = call or FakeCall(family.chat_id)
    await handler.tap(
        call,
        kb.EntryCB(
            action=action, entry_id=entry_id, view=view, offset=offset, value=value
        ),
        session,
        family,
        bot,
    )
    return call


# --- Вход в карточку ---------------------------------------------------------


@pytest.mark.asyncio
async def test_list_offers_a_card_button(session, family, task):
    """Рядом с «закрыть» появилась вторая кнопка — вход в карточку."""
    _, markup = await views._render_page(session, family, "tasks", 0)
    assert [b.text for b in markup.inline_keyboard[0]] == ["✅ 1", "✏️ 1"]

    opener = markup.inline_keyboard[0][1]
    unpacked = kb.EntryCB.unpack(opener.callback_data)
    assert (unpacked.action, unpacked.entry_id, unpacked.view) == (
        "open",
        task.id,
        "tasks",
    )


@pytest.mark.asyncio
async def test_card_shows_the_entry_and_its_author(session, family, task, bot):
    call = await _tap("open", task.id, session, family, bot)
    assert "позвонить маме" in call.screen
    assert "Аня" in call.screen  # автор подгружен, а не «кто-то»
    assert kb.BTN_ENTRY_TEXT in call.labels
    assert kb.BTN_ENTRY_DELETE in call.labels


@pytest.mark.asyncio
async def test_back_returns_to_the_page_it_came_from(session, family, anya, bot):
    """«← Назад» на странице событий возвращает события, а не задачи."""
    event = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="event",
        title="утренник",
        due_at=_msk(2026, 9, 1, 10, 0),
    )
    call = await _tap("back", event.id, session, family, bot, view="events")
    assert "События" in call.screen
    assert "утренник" in call.screen


# --- Правка текста -----------------------------------------------------------


@pytest.mark.asyncio
async def test_text_edit_by_reply(session, family, task, bot):
    call = await _tap("text", task.id, session, family, bot)
    assert texts.ENTRY_ASK_TEXT in call.screen
    assert handler._awaits(FakeReply(family.chat_id, "позвонить бабушке"))

    await handler.take_reply(
        FakeReply(family.chat_id, "позвонить бабушке"), session, family, bot
    )
    await session.refresh(task)
    assert task.title == "позвонить бабушке"
    # Карточка перерисована правкой сообщения, а не новым сообщением
    assert bot.edited and "позвонить бабушке" in bot.edited[-1][2]


@pytest.mark.asyncio
async def test_text_edit_updates_the_live_reminder(session, family, anya, task, bot):
    """Текст напоминания — снимок заголовка, и он обязан переехать следом.

    Иначе в 19:00 бот скажет про маму, хотя запись давно про бабушку.
    """
    live = await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=anya.id,
        text=task.title,
        fire_at=_msk(2026, 8, 28, 18, 0),
        entry_id=task.id,
    )
    await _tap("text", task.id, session, family, bot)
    await handler.take_reply(
        FakeReply(family.chat_id, "позвонить бабушке"), session, family, bot
    )
    await session.refresh(live)
    assert live.text == "позвонить бабушке"


@pytest.mark.asyncio
async def test_empty_text_keeps_waiting(session, family, task, bot):
    """Правка не состоялась — ожидание не снимаем, иначе отвечать некуда."""
    await _tap("text", task.id, session, family, bot)
    reply = FakeReply(family.chat_id, "   ")
    await handler.take_reply(reply, session, family, bot)

    assert reply.replies == [texts.ENTRY_BAD_TEXT]
    assert handler._awaits(FakeReply(family.chat_id, "ещё раз"))
    await session.refresh(task)
    assert task.title == "позвонить маме"


@pytest.mark.asyncio
async def test_back_stops_waiting_for_a_reply(session, family, task, bot):
    """Вернулись в карточку — ответ реплаем больше не считается правкой."""
    call = FakeCall(family.chat_id)
    await _tap("text", task.id, session, family, bot, call=call)
    assert handler._awaits(FakeReply(family.chat_id, "что-то"))

    await _tap("open", task.id, session, family, bot, call=call)
    assert not handler._awaits(FakeReply(family.chat_id, "что-то"))


# --- Правка срока ------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_button_keeps_the_time_of_day(session, family, task, bot):
    call = await _tap("day", task.id, session, family, bot, value=1)
    await session.refresh(task)

    tomorrow = tu.local_today(family.tz) + timedelta(days=1)
    local = tu.to_local(task.due_at, family.tz)
    assert local.date() == tomorrow
    assert local.time() == time(19, 0)
    assert call.answers  # человеку сказали, куда переехало


@pytest.mark.asyncio
async def test_entry_without_a_date_becomes_all_day(session, family, anya, bot):
    """У записи без срока времени суток нет — брать его неоткуда.

    Регрессия: наивный перенос звал бы `to_local(None)` и падал.
    """
    note = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="без срока"
    )
    assert note.due_at is None

    await _tap("day", note.id, session, family, bot, value=1)
    await session.refresh(note)

    assert note.all_day is True
    assert tu.to_local(note.due_at, family.tz).time() == time(0, 0)


@pytest.mark.asyncio
async def test_clearing_the_date_also_clears_all_day(session, family, anya, bot):
    """Флаг «весь день» без дня — мусор: следующий перенос уехал бы в ветку all-day."""
    event = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="event",
        title="выходной",
        due_at=_msk(2026, 9, 1),
        all_day=True,
    )
    await _tap("nodate", event.id, session, family, bot, view="events")
    await session.refresh(event)

    assert event.due_at is None
    assert event.all_day is False


@pytest.mark.asyncio
async def test_no_date_button_hidden_when_there_is_no_date(session, family, anya, bot):
    """Кнопка, которая заведомо ничего не изменит, обещает действие впустую."""
    bare = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="без срока"
    )
    call = await _tap("date", bare.id, session, family, bot)
    assert kb.BTN_ENTRY_NO_DATE not in call.labels

    dated = await _tap("date", (await _with_date(session, family, anya)).id,
                       session, family, bot)
    assert kb.BTN_ENTRY_NO_DATE in dated.labels


async def _with_date(session, family, anya):
    return await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="со сроком",
        due_at=_msk(2026, 9, 2, 8, 0),
    )


@pytest.mark.asyncio
async def test_other_day_by_reply_takes_only_the_day(session, family, task, bot):
    """Время суток у записи своё — придуманное `dateparser` «сейчас» не берём."""
    await _tap("other", task.id, session, family, bot)
    await handler.take_reply(
        FakeReply(family.chat_id, "в понедельник"), session, family, bot
    )
    await session.refresh(task)
    assert tu.to_local(task.due_at, family.tz).time() == time(19, 0)


@pytest.mark.asyncio
async def test_unparsed_date_keeps_waiting(session, family, task, bot):
    await _tap("other", task.id, session, family, bot)
    reply = FakeReply(family.chat_id, "когда-нибудь потом")
    await handler.take_reply(reply, session, family, bot)

    assert reply.replies == [texts.ENTRY_BAD_DATE]
    assert handler._awaits(FakeReply(family.chat_id, "завтра"))


@pytest.mark.asyncio
async def test_reply_can_clear_the_date(session, family, task, bot):
    await _tap("other", task.id, session, family, bot)
    await handler.take_reply(
        FakeReply(family.chat_id, "без даты"), session, family, bot
    )
    await session.refresh(task)
    assert task.due_at is None


@pytest.mark.asyncio
async def test_move_switches_off_the_one_off_reminder(session, family, anya, task, bot):
    """Старое напоминание выстрелило бы догонкой в ближайший тик."""
    one_off = await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=anya.id,
        text=task.title,
        fire_at=_msk(2026, 8, 28, 18, 0),
        entry_id=task.id,
    )
    await _tap("day", task.id, session, family, bot, value=7)
    await session.refresh(one_off)
    assert one_off.active is False


@pytest.mark.asyncio
async def test_move_leaves_the_recurring_reminder_alone(
    session, family, anya, task, bot
):
    """Тихо убить серию хуже, чем оставить её как есть."""
    series = await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=anya.id,
        text=task.title,
        fire_at=_msk(2026, 8, 28, 18, 0),
        entry_id=task.id,
        rrule="FREQ=WEEKLY;BYDAY=FR",
    )
    await _tap("day", task.id, session, family, bot, value=7)
    await session.refresh(series)
    assert series.active is True


# --- Удаление ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_asks_first(session, family, task, bot):
    call = await _tap("del", task.id, session, family, bot)
    assert "позвонить маме" in call.screen
    assert kb.BTN_ENTRY_DELETE_YES in call.labels

    await session.refresh(task)
    assert task.status == "open"  # первый тап ничего не изменил


@pytest.mark.asyncio
async def test_confirmed_delete_hides_the_entry_everywhere(
    session, family, anya, task, bot
):
    await _tap("yes", task.id, session, family, bot)
    await session.refresh(task)
    assert task.status == repo.ARCHIVED

    page, _ = await views._render_page(session, family, "tasks", 0)
    assert "позвонить маме" not in page
    assert not await repo.search_entries(session, family.id, "маме")

    day, _ = await digest.build_day(session, family, _msk(2026, 8, 28, 12, 0))
    assert "позвонить маме" not in day
    assert not await repo.overdue_entries(session, family.id, _msk(2026, 9, 1))
    assert not await review.overdue(session, family, _msk(2026, 9, 1))


@pytest.mark.asyncio
async def test_delete_offers_an_undo(session, family, task, bot):
    """Подтверждение спасает от промаха пальцем, но не от «удалил не ту»."""
    call = await _tap("yes", task.id, session, family, bot)
    assert kb.BTN_ENTRY_UNDO in call.labels

    undo = [
        b
        for row in call.markup.inline_keyboard
        for b in row
        if b.text == kb.BTN_ENTRY_UNDO
    ][0]
    unpacked = kb.EntryCB.unpack(undo.callback_data)
    assert (unpacked.action, unpacked.entry_id) == ("undo", task.id)

    await _tap("undo", task.id, session, family, bot)
    await session.refresh(task)
    assert task.status == "open"


@pytest.mark.asyncio
async def test_undo_on_a_live_entry_is_refused(session, family, task, bot):
    call = await _tap("undo", task.id, session, family, bot)
    assert call.alert == texts.ENTRY_GONE


@pytest.mark.asyncio
async def test_second_delete_is_refused(session, family, task, bot):
    await _tap("yes", task.id, session, family, bot)
    call = await _tap("yes", task.id, session, family, bot)
    assert call.alert == texts.ENTRY_GONE


@pytest.mark.asyncio
async def test_reminder_of_a_deleted_entry_goes_quiet(
    session, family, anya, task, bot
):
    """Гасить руками не нужно: выборка тикера требует `status == 'open'`."""
    await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=anya.id,
        text=task.title,
        fire_at=_msk(2026, 8, 28, 18, 0),
        entry_id=task.id,
    )
    assert await repo.due_reminders(session, _msk(2026, 8, 29))

    await _tap("yes", task.id, session, family, bot)
    assert not await repo.due_reminders(session, _msk(2026, 8, 29))


# --- Чужой чат ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_chat_cannot_touch_the_entry(session, family, task, bot):
    stranger = await repo.get_or_create_family(session, -1002, "Чужие")
    call = await _tap("yes", task.id, session, stranger, bot)

    assert call.alert == texts.ENTRY_GONE
    await session.refresh(task)
    assert task.status == "open"


@pytest.mark.asyncio
async def test_another_chat_never_sees_the_title(session, family, task, bot):
    """`repo` стережёт правку, хендлер — чтение: карточка показывает заголовок."""
    stranger = await repo.get_or_create_family(session, -1003, "Чужие")
    call = await _tap("open", task.id, session, stranger, bot)

    assert "позвонить маме" not in call.screen
    assert call.alert == texts.ENTRY_GONE


@pytest.mark.asyncio
async def test_reply_from_another_chat_is_refused(session, family, task, bot):
    """Проверка семьи нужна и на реплае, а не только на тапе."""
    await _tap("text", task.id, session, family, bot)
    stranger = await repo.get_or_create_family(session, -1004, "Чужие")

    reply = FakeReply(family.chat_id, "переписано чужими")
    await handler.take_reply(reply, session, stranger, bot)

    assert reply.replies == [texts.ENTRY_GONE]
    await session.refresh(task)
    assert task.title == "позвонить маме"


# --- Панель дня --------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_is_woken_by_edit_and_by_delete(
    session, family, task, bot, monkeypatch
):
    """Заголовок и срок видны в панели — она обязана узнать об изменении."""
    woken: list[int] = []
    monkeypatch.setattr(
        handler.panel, "schedule", lambda bot, family_id, mid=None: woken.append(family_id)
    )

    await _tap("day", task.id, session, family, bot, value=1)
    await _tap("yes", task.id, session, family, bot)
    assert woken == [family.id, family.id]


# --- Выгрузка ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_keeps_deleted_entries_but_marks_them(
    session, family, task, bot
):
    """Выгрузка — единственное место, где удалённое видно. Врать там нельзя."""
    await _tap("yes", task.id, session, family, bot)
    rows = await repo.all_entries(session, family.id)
    assert [e.id for e in rows] == [task.id]

    csv = export.to_csv(rows, family.tz).decode("utf-8-sig")
    assert "удалена" in csv

    md = export.to_markdown(rows, family.tz, "Семья", date(2026, 8, 28)).decode()
    assert "[ ]" not in md  # удалённая не должна выглядеть открытой


# --- Список покупок ----------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_item_cannot_be_resurrected_by_a_tap(session, family, anya):
    """Панелей списка в чате лежит стопка, и старые кнопки живут вечно.

    Без проверки статуса ветка «снять галку» воскресила бы удалённый пункт.
    """
    lst = await repo.get_or_create_active_list(session, family.id)
    (item,) = await repo.add_items(
        session, family_id=family.id, author_id=anya.id, list_id=lst.id, titles=["соль"]
    )
    await repo.archive_entry(session, item.id, family.id)

    assert await repo.toggle_bought(session, item.id, family.id, anya.id) is None
    await session.refresh(item)
    assert item.status == repo.ARCHIVED
    assert await repo.list_items(session, lst.id) == []


@pytest.mark.asyncio
async def test_list_with_everything_deleted_stays_open(session, family, anya):
    """Опустевший список — это пустой список, а не закрытый.

    Не вычти удалённые из `total`, и список, из которого выбросили всё, уехал
    бы в «🧹 Список закрыт»: открытых не осталось, а всего больше нуля. Пустой
    закрытым не считается — ровно как только что созданный.
    """
    lst = await repo.get_or_create_active_list(session, family.id)
    items = await repo.add_items(
        session,
        family_id=family.id,
        author_id=anya.id,
        list_id=lst.id,
        titles=["хлеб", "соль"],
    )
    for item in items:
        await repo.archive_entry(session, item.id, family.id)

    assert await repo.sync_list_archived(session, lst) is False
    assert lst.archived is False


@pytest.mark.asyncio
async def test_list_still_closes_when_everything_is_bought(session, family, anya):
    """Обратная половина: без неё фильтр «упростят» и автоархив умрёт молча."""
    lst = await repo.get_or_create_active_list(session, family.id)
    items = await repo.add_items(
        session,
        family_id=family.id,
        author_id=anya.id,
        list_id=lst.id,
        titles=["хлеб", "соль"],
    )
    for item in items:
        await repo.toggle_bought(session, item.id, family.id, anya.id)

    assert await repo.sync_list_archived(session, lst) is True


# --- Проводка ----------------------------------------------------------------


def test_entry_router_stands_before_the_wizard():
    """Реплай на сообщение бота `IsTrigger` считает обращением.

    Стой роутер позади `capture`, новый текст записи уехал бы в модель отдельным
    платным запросом, а посреди `/new` стал бы заголовком новой записи.
    """
    from bot.handlers import capture, entry, new_entry, routers

    assert routers.index(entry.router) < routers.index(new_entry.router)
    assert routers.index(entry.router) < routers.index(capture.router)


def test_entry_router_drops_a_hanging_wizard():
    """Как и все роутеры, стоящие раньше мастера."""
    from bot.handlers import entry
    from bot.middlewares import drop_wizard_state

    assert drop_wizard_state in entry.router.message.middleware


@pytest.mark.asyncio
async def test_page_survives_an_edit(session, family, anya, bot, monkeypatch):
    """После правки «← Назад» обязан вернуть на ту же страницу.

    Регрессия: пока смещение ехало в том же поле, что и сдвиг в днях, после
    переноса оно терялось и человека выбрасывало на первую страницу. То же
    было на пути правки реплаем — там колбэка нет вовсе, и смещение пришлось
    класть в `_pending`.
    """
    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    made = []
    for i in range(4):
        made.append(
            await repo.create_entry(
                session,
                family_id=family.id,
                author_id=anya.id,
                kind="task",
                title=f"задача {i}",
                due_at=_msk(2026, 9, 1 + i, 9, 0),
            )
        )
    third = made[2]  # первая на второй странице

    # Кнопка со второй страницы несёт смещение
    _, markup = await views._render_page(session, family, "tasks", 2)
    opener = kb.EntryCB.unpack(markup.inline_keyboard[0][1].callback_data)
    assert (opener.entry_id, opener.offset) == (third.id, 2)

    # Перенос кнопкой смещение не теряет
    call = await _tap("day", third.id, session, family, bot, offset=2, value=1)
    back = [
        kb.EntryCB.unpack(b.callback_data)
        for row in call.markup.inline_keyboard
        for b in row
        if b.text == kb.BTN_ENTRY_BACK
    ]
    assert back and back[0].offset == 2

    # И правка реплаем тоже
    await _tap("text", third.id, session, family, bot, offset=2)
    await handler.take_reply(
        FakeReply(family.chat_id, "переписано"), session, family, bot
    )
    markup = bot.edit_kwargs[-1]["reply_markup"]
    back = [
        kb.EntryCB.unpack(b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.text == kb.BTN_ENTRY_BACK
    ]
    assert back and back[0].offset == 2
