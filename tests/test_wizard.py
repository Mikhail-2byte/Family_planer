"""Мастер /new — этап 1.10: разбор даты и сохранение."""

from datetime import date, datetime, timedelta

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import texts
from bot.db import repo
from bot.handlers import new_entry
from bot.handlers.new_entry import ALLDAY_ANCHOR, _parse_day, _save
from bot.middlewares import drop_wizard_state
from bot.services import timeutil as tu
from tests.conftest import FakeBot

TODAY = date(2026, 8, 27)
MSK = "Europe/Moscow"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("28.08", date(2026, 8, 28)),
        ("28.08.2026", date(2026, 8, 28)),
        ("28.08.26", date(2026, 8, 28)),
        ("28/08", date(2026, 8, 28)),
        (" 1.9 ", date(2026, 9, 1)),
        # Без года прошедшая дата означает следующий год, а не «вчера»
        ("01.03", date(2027, 3, 1)),
        # С явным годом прошлое остаётся прошлым — человек так и написал
        ("01.03.2026", date(2026, 3, 1)),
    ],
)
def test_parse_day(raw, expected):
    assert _parse_day(raw, TODAY) == expected


@pytest.mark.parametrize("raw", ["завтра", "32.01", "", "8", "13.13"])
def test_parse_day_rejects_garbage(raw):
    assert _parse_day(raw, TODAY) is None


# --- сохранение: напоминание не должно выстреливать в прошлое -----------------


class FakeState:
    """Минимальный FSMContext: мастеру от него нужны только data и clear."""

    def __init__(self, **data):
        self._data = data
        self.cleared = False

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self.cleared = True


class FakeMessage:
    def __init__(self, chat_id=-1001):
        self.chat = type("Chat", (), {"id": chat_id})()
        self.message_id = 1  # по нему панель считает, далеко ли уехала
        self.replies: list[str] = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)


async def _save_with(session, family, anya, **data):
    message = FakeMessage()
    # `bot` мастеру нужен только ради панели списка на покупке (4.5);
    # здесь виды другие, но аргумент обязателен
    await _save(message, FakeState(**data), session, family, anya, FakeBot())
    return message.replies


def _in_days(n: int) -> str:
    return (tu.local_today(MSK) + timedelta(days=n)).isoformat()


@pytest.mark.asyncio
async def test_wizard_does_not_create_a_reminder_in_the_past(session, family, anya):
    """Иначе тикер отработает догонкой и выстрелит в ближайший тик.

    `/remind` этот случай отвергает явно — мастер обязан вести себя так же.
    """
    replies = await _save_with(
        session, family, anya,
        kind="task", title="Позвонить в поликлинику",
        day=_in_days(-1), at="09:00", remind_before=15,
    )

    assert await repo.due_reminders(session) == [], "напоминание в прошлом не заводим"
    assert "Позвонить в поликлинику" in replies[0]  # сама запись сохранена
    assert texts.REMIND_PAST.split("{")[0] in replies[0]  # и человеку сказали, почему


@pytest.mark.asyncio
async def test_wizard_creates_a_future_reminder(session, family, anya):
    await _save_with(
        session, family, anya,
        kind="task", title="Забрать посылку",
        day=_in_days(1), at="19:00", remind_before=15,
    )

    due = await repo.due_reminders(session, tu.to_utc(datetime(2030, 1, 1), MSK))
    assert len(due) == 1
    assert tu.to_local(due[0].fire_at, MSK).strftime("%H:%M") == "18:45"


@pytest.mark.asyncio
async def test_allday_reminder_counts_from_the_morning_anchor(session, family, anya):
    """У записи на весь день срок — полночь; «утром» от неё дало бы 00:00."""
    await _save_with(
        session, family, anya,
        kind="event", title="День рождения",
        day=_in_days(3), all_day=True, remind_before=0,
    )

    due = await repo.due_reminders(session, tu.to_utc(datetime(2030, 1, 1), MSK))
    assert len(due) == 1
    assert tu.to_local(due[0].fire_at, MSK).time() == ALLDAY_ANCHOR


@pytest.mark.asyncio
async def test_allday_evening_before_lands_on_the_previous_day(session, family, anya):
    await _save_with(
        session, family, anya,
        kind="event", title="Утренник",
        day=_in_days(3), all_day=True, remind_before=840,
    )

    due = await repo.due_reminders(session, tu.to_utc(datetime(2030, 1, 1), MSK))
    fire_local = tu.to_local(due[0].fire_at, MSK)
    assert fire_local.date() == tu.local_today(MSK) + timedelta(days=2)
    assert fire_local.strftime("%H:%M") == "19:00"


@pytest.mark.asyncio
async def test_entry_without_a_date_gets_no_reminder(session, family, anya):
    await _save_with(session, family, anya, kind="note", title="Идея", remind_before=0)
    assert await repo.due_reminders(session, tu.to_utc(datetime(2030, 1, 1), MSK)) == []


# --- мастер целиком: от первой кнопки до сохранённой записи -------------------


class WizardMessage(FakeMessage):
    """Сообщение, которое мастер перерисовывает и удаляет."""

    def __init__(self, text: str = "", chat_id: int = -1001):
        super().__init__(chat_id)
        self.text = text
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeCall:
    """CallbackQuery ровно в той части, которой пользуется мастер."""

    def __init__(self, data: str, message: WizardMessage):
        self.data = data
        self.message = message
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def _fsm() -> FSMContext:
    """Настоящий FSMContext на памяти: FakeState хранит данные, но не состояние."""
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=-1001, user_id=222),
    )


@pytest.mark.asyncio
async def test_wizard_goes_from_buttons_to_a_saved_entry(session, family, anya, bot):
    """Критерий 1.10: запись создаётся кнопками от начала до конца."""
    state = _fsm()
    card = WizardMessage()

    await new_entry.start_wizard(card, state)
    assert await state.get_state() == new_entry.New.kind.state

    await new_entry.pick_kind(FakeCall("new:kind:task", card), state)
    await new_entry.take_title(WizardMessage("Забрать посылку"), state)
    assert await state.get_state() == new_entry.New.day.state

    await new_entry.pick_day(
        FakeCall("new:day:1", card), state, session, family, anya, bot
    )
    await new_entry.pick_time(
        FakeCall("new:at:19:00", card), state, session, family, anya
    )
    assert await state.get_state() == new_entry.New.remind.state

    await new_entry.pick_remind(
        FakeCall("new:rem:15", card), state, session, family, anya, bot
    )

    assert card.deleted, "карточку мастера убираем, а не оставляем висеть"
    assert await state.get_state() is None

    entries, total = await repo.entries_by_kind(
        session, family.id, "task", status="open", limit=10, offset=0
    )
    assert total == 1
    saved = entries[0]
    assert saved.title == "Забрать посылку"
    assert not saved.all_day
    local = tu.to_local(saved.due_at, MSK)
    assert local.date() == tu.local_today(MSK) + timedelta(days=1)
    assert local.strftime("%H:%M") == "19:00"

    due = await repo.due_reminders(session, tu.to_utc(datetime(2030, 1, 1), MSK))
    assert len(due) == 1
    assert tu.to_local(due[0].fire_at, MSK).strftime("%H:%M") == "18:45"


# --- мастер не виснет, когда сообщение перехватил просмотр --------------------


@pytest.mark.asyncio
async def test_view_in_the_middle_of_the_wizard_drops_the_state():
    """Роутер views стоит раньше мастера: без сброса следующая реплика человека
    молча стала бы заголовком записи."""
    state = _fsm()
    await state.set_state(new_entry.New.title)
    message = WizardMessage("📅 Сегодня")
    seen = []

    async def handler(event, data):
        seen.append(await data["state"].get_state())
        return "показал день"

    result = await drop_wizard_state(handler, message, {"state": state})

    assert result == "показал день"
    assert seen == [None], "просмотр работает уже с чистым состоянием"
    assert await state.get_state() is None
    assert message.replies == [texts.WIZARD_DROPPED]


@pytest.mark.asyncio
async def test_message_outside_the_wizard_is_not_disturbed():
    message = WizardMessage("привет")

    async def handler(event, data):
        return "ok"

    assert await drop_wizard_state(handler, message, {"state": _fsm()}) == "ok"
    assert message.replies == []


# --- /cancel вне мастера ------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_outside_the_wizard_answers():
    message = WizardMessage("/cancel")
    await new_entry.cmd_cancel_idle(message)
    assert message.replies == [new_entry.NOTHING_TO_CANCEL]


def test_cancel_inside_the_wizard_is_registered_first():
    """Наоборот — и `/cancel` в мастере ответит «нечего отменять», не сбросив FSM."""
    names = [h.callback.__name__ for h in new_entry.router.message.handlers]
    assert names.index("cmd_cancel") < names.index("cmd_cancel_idle")


def test_state_drop_hangs_on_every_router_but_the_wizard():
    """Роутер, стоящий раньше мастера и оставшийся без middleware, вернёт
    зависание: сообщение перехвачено, состояние живо."""
    from bot.handlers import admin, remind, views

    for module in (admin, views, remind):
        assert drop_wizard_state in module.router.message.middleware
    assert drop_wizard_state not in new_entry.router.message.middleware


@pytest.mark.asyncio
async def test_wizard_shopping_refreshes_the_list_panel(session, family, anya, bot):
    """Тот же долг у мастера: покупка легла в список — панель обязана ожить."""
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.set_list_panel(session, lst, 100)

    message = FakeMessage()
    await _save(
        message,
        FakeState(kind="shopping", title="Сыр"),
        session,
        family,
        anya,
        bot,
    )

    assert bot.edited, "панель списка не тронута"
    assert bot.edited[-1][1] == 100
    assert "Сыр" in bot.edited[-1][2]
