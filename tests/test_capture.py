"""Карточка подтверждения и сохранение разбора, шаг 3a.6."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from bot import keyboards as kb
from bot import texts
from bot.db.models import Entry, Reminder
from bot.handlers import capture
from bot.services import llm, parsing
from bot.services import timeutil as tu

MSK = "Europe/Moscow"


# --- заглушки -----------------------------------------------------------------


class FakeCard:
    """Сообщение бота с карточкой: у него правят текст, а не шлют новое."""

    def __init__(self, message_id: int, chat_id: int):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeMessage:
    """Исходная фраза человека. `answer` возвращает карточку, как настоящий."""

    def __init__(self, chat_id: int = -1001, message_id: int = 500):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = message_id
        self.replies: list[tuple[str, dict]] = []
        self.cards: list[FakeCard] = []
        # Карточки нумеруются от исходного сообщения: два разбора подряд
        # должны получить разные message_id, иначе черновики перетрут друг друга
        self._next_id = message_id * 10

    async def answer(self, text: str, **kwargs) -> FakeCard:
        self._next_id += 1
        self.replies.append((text, kwargs))
        card = FakeCard(self._next_id, self.chat.id)
        self.cards.append(card)
        return card

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.replies]


class FakeCall:
    def __init__(self, message: FakeCard):
        self.message = message
        self.answers: list[tuple[str | None, dict]] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append((text, kwargs))

    @property
    def alert(self) -> str | None:
        return self.answers[0][0] if self.answers else None


@pytest.fixture(autouse=True)
def _drafts():
    """Черновики живут в модульном словаре — между тестами он течёт."""
    capture._drafts.clear()
    yield
    capture._drafts.clear()


@pytest.fixture
def answer(monkeypatch):
    """Подменяет модель. Возвращает список запросов, ушедших бы в OpenRouter."""
    calls: list[tuple[str, str]] = []

    def use(reply):
        async def fake_ask(system, user, **kwargs):
            calls.append((system, user))
            return reply

        monkeypatch.setattr(llm, "ask", fake_ask)
        return calls

    return use


def _in(hours: float):
    """Местное время семьи через `hours` часов — то, что вернула бы модель."""
    local = tu.to_local(tu.now_utc(), MSK) + timedelta(hours=hours)
    return local.replace(microsecond=0).isoformat()


def _reply(*items, intent="create"):
    return {"intent": intent, "items": list(items)}


def _item(title="Купить молоко", **fields):
    return {"kind": "shopping", "title": title, "confidence": 0.9, **fields}


@pytest_asyncio.fixture
async def carded(session, family, anya, answer):
    """Прогон до карточки: возвращает (исходное сообщение, карточка)."""

    async def show(reply, message_id: int = 500):
        answer(reply)
        message = FakeMessage(chat_id=family.chat_id, message_id=message_id)
        await capture.capture(message, "купить молоко", session, family)
        return message, message.cards[0] if message.cards else None

    return show


# --- развилка по intent -------------------------------------------------------


@pytest.mark.asyncio
async def test_chitchat_says_nothing(carded):
    """Критерий закрытия этапа: «а что там с отпуском» — бот молчит."""
    message, card = await carded(_reply(intent="chitchat"))
    assert message.replies == []
    assert card is None
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_llm_failure_answers_once_and_shows_no_card(carded):
    """Сеть, ключ, отказ провайдера — наружу приходят одинаково, как `None`."""
    message, _ = await carded(None)
    assert message.texts == [texts.CAPTURE_FAILED]
    assert capture._drafts == {}  # ответ ушёл, но черновика за ним нет


@pytest.mark.parametrize("intent", ["query", "complete"])
@pytest.mark.asyncio
async def test_unsupported_intent_gets_a_hint(carded, intent):
    """К боту обратились явно — молчание читалось бы как поломка."""
    message, _ = await carded(_reply(intent=intent))
    assert message.texts == [texts.CAPTURE_NOT_YET]
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_create_without_items_offers_the_wizard(carded):
    message, _ = await carded(_reply())
    assert message.texts == [texts.CAPTURE_EMPTY]
    assert capture._drafts == {}


# --- карточка -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_shows_parse_and_two_buttons(carded):
    message, card = await carded(_reply(_item(due_at=_in(4))))
    text, kwargs = message.replies[0]

    assert texts.CAPTURE_ASK in text
    assert "Купить молоко" in text
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == [kb.BTN_SAVE, kb.BTN_CANCEL]
    assert (message.chat.id, card.message_id) in capture._drafts


@pytest.mark.asyncio
async def test_draft_points_at_the_original_message(carded, family):
    message, card = await carded(_reply(_item()))
    draft = capture._drafts[(message.chat.id, card.message_id)]
    # Не id карточки: ссылка 3a.7 должна вести к фразе человека
    assert draft.source_message_id == message.message_id
    assert draft.family_id == family.id


@pytest.mark.asyncio
async def test_low_confidence_is_flagged_in_the_card(carded):
    message, _ = await carded(_reply(_item(confidence=0.2)))
    assert texts.CAPTURE_UNCERTAIN in message.texts[0]


@pytest.mark.asyncio
async def test_prompt_carries_family_context(session, family, anya, answer):
    calls = answer(_reply(intent="chitchat"))
    await capture.capture(FakeMessage(), "купить молоко", session, family)

    system, user = calls[0]
    assert "Аня" in system  # имена участников
    assert family.tz in system
    assert user == "купить молоко"  # payload без признака обращения


@pytest.mark.asyncio
async def test_oldest_draft_is_evicted(carded, monkeypatch):
    """Иначе брошенные карточки копятся всё время работы бота."""
    monkeypatch.setattr(capture, "MAX_DRAFTS", 3)
    for i in range(5):
        await carded(_reply(_item()), message_id=500 + i)
    assert len(capture._drafts) == 3


# --- сохранение ---------------------------------------------------------------


async def _tap(action, card, session, family, member, bot):
    call = FakeCall(card)
    await capture.tap(
        call, kb.CaptureCB(action=action), session, family, member, bot
    )
    return call


async def _entries(session, family):
    found = await session.execute(
        select(Entry).where(Entry.family_id == family.id).order_by(Entry.id)
    )
    return list(found.scalars())


async def _reminders(session, family):
    found = await session.execute(
        select(Reminder).where(Reminder.family_id == family.id).order_by(Reminder.id)
    )
    return list(found.scalars())


@pytest.mark.asyncio
async def test_save_writes_entry_with_link_to_the_original(
    carded, session, family, anya, bot
):
    message, card = await carded(
        _reply(_item(due_at=_in(20), reminders=[{"at": _in(19)}]))
    )
    await _tap("save", card, session, family, anya, bot)

    entry = (await _entries(session, family))[0]
    assert entry.kind == "shopping"
    assert entry.title == "Купить молоко"
    assert entry.source_chat_id == family.chat_id
    assert entry.source_message_id == message.message_id

    # Карточка переписана на подтверждение, и в нём — рабочая ссылка (3a.7)
    saved = card.edits[0]
    assert texts.SAVED in saved
    assert f"/{message.message_id}" in saved and "t.me/c/" in saved
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_save_creates_the_reminder(carded, session, family, anya, bot):
    _, card = await carded(_reply(_item(due_at=_in(20), reminders=[{"at": _in(19)}])))
    await _tap("save", card, session, family, anya, bot)

    reminder = (await _reminders(session, family))[0]
    assert reminder.text == "Купить молоко"
    assert reminder.entry_id == (await _entries(session, family))[0].id
    assert reminder.fire_at > tu.now_utc()
    assert reminder.rrule is None


@pytest.mark.asyncio
async def test_past_reminder_is_refused_not_fired(
    carded, session, family, anya, bot
):
    """Иначе тикер отработает это догонкой и выстрелит в ближайший тик."""
    _, card = await carded(_reply(_item(due_at=_in(20), reminders=[{"at": _in(-3)}])))
    await _tap("save", card, session, family, anya, bot)

    assert await _reminders(session, family) == []
    assert len(await _entries(session, family)) == 1  # запись всё равно сохранена
    assert "уже прошло" in card.edits[0]


@pytest.mark.asyncio
async def test_recurring_reminder_is_created(carded, session, family, anya, bot):
    """Тикер повторы умеет с этапа 2, но завести их до сих пор было нечем."""
    _, card = await carded(
        _reply(_item(title="Вынести мусор", rrule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=19"))
    )
    await _tap("save", card, session, family, anya, bot)

    reminder = (await _reminders(session, family))[0]
    assert reminder.rrule == "FREQ=WEEKLY;BYDAY=TU;BYHOUR=19"
    assert reminder.fire_at > tu.now_utc()
    # У правила без BYSECOND секунды берутся из якоря, и без обрезки серия
    # навсегда осталась бы со случайными «19:00:37» от момента создания
    assert reminder.fire_at.second == 0
    assert tu.to_local(reminder.fire_at, MSK).hour == 19
    assert tu.to_local(reminder.fire_at, MSK).weekday() == 1  # вторник
    # Отработку повторяющегося помечает fire_at, а не sent_at
    assert reminder.sent_at is None


@pytest.mark.asyncio
async def test_unusable_rrule_is_reported_not_swallowed(
    carded, session, family, anya, bot
):
    _, card = await carded(_reply(_item(rrule="FREQ=НИКОГДА")))
    await _tap("save", card, session, family, anya, bot)

    assert await _reminders(session, family) == []
    assert len(await _entries(session, family)) == 1
    assert "не понял" in card.edits[0]


@pytest.mark.asyncio
async def test_several_items_become_several_entries(
    carded, session, family, anya, bot
):
    """«Купи молока и хлеба» — это два items, и терять второй нельзя."""
    _, card = await carded(_reply(_item(title="Молоко"), _item(title="Хлеб")))
    await _tap("save", card, session, family, anya, bot)

    assert [e.title for e in await _entries(session, family)] == ["Молоко", "Хлеб"]


@pytest.mark.asyncio
async def test_card_numbers_several_items(carded):
    message, _ = await carded(_reply(_item(title="Молоко"), _item(title="Хлеб")))
    assert "1. " in message.texts[0] and "2. " in message.texts[0]


@pytest.mark.asyncio
async def test_overlong_parse_is_refused_before_anything_is_saved(carded):
    """Telegram на 4096 символах отвечает отказом, а не обрезает сам.

    Обрезать карточку нельзя — человек подтвердил бы кнопкой то, чего не видел,
    — поэтому такой разбор отклоняется целиком.
    """
    huge = [_item(title="я" * 500, body="б" * 1000) for _ in range(10)]
    message, _ = await carded(_reply(*huge))

    assert message.texts == [texts.CAPTURE_TOO_LONG]
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_long_confirmation_is_trimmed_not_dropped(
    session, family, anya, bot, monkeypatch
):
    """Записи уже в базе — подрезать можно только эхо, и молчать нельзя.

    Отказ Telegram по длине оставил бы человека с «часиком» над сохранёнными
    записями, и он нажал бы «Сохранить» ещё раз.
    """
    monkeypatch.setattr(texts, "MESSAGE_LIMIT", 300)
    items = [
        parsing.Item(kind="task", title=f"дело {i}", confidence=0.9) for i in range(10)
    ]
    card = FakeCard(901, family.chat_id)
    capture._drafts[(family.chat_id, 901)] = capture.Draft(family.id, items, 500)

    await _tap("save", card, session, family, anya, bot)

    assert len(await _entries(session, family)) == 10  # сохранены все
    assert len(card.edits[0]) <= 300
    assert "…и ещё" in card.edits[0]


# --- отказы -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_saves_nothing(carded, session, family, anya, bot):
    _, card = await carded(_reply(_item()))
    await _tap("cancel", card, session, family, anya, bot)

    assert await _entries(session, family) == []
    assert card.edits == [texts.CAPTURE_CANCELLED]
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_stale_card_refuses(session, family, anya, bot):
    """Перезапуск бота обрывает черновики так же, как состояние мастера."""
    call = await _tap("save", FakeCard(777, family.chat_id), session, family, anya, bot)

    assert call.alert == texts.CAPTURE_STALE
    assert await _entries(session, family) == []


@pytest.mark.asyncio
async def test_double_tap_saves_once(carded, session, family, anya, bot):
    """Двое могут нажать «Сохранить» на одной карточке почти одновременно."""
    _, card = await carded(_reply(_item()))
    await _tap("save", card, session, family, anya, bot)
    second = await _tap("save", card, session, family, anya, bot)

    assert second.alert == texts.CAPTURE_STALE
    assert len(await _entries(session, family)) == 1


@pytest.mark.asyncio
async def test_card_of_another_family_refuses(
    carded, session, family, anya, bot
):
    """Изоляция по family_id — инвариант проекта."""
    _, card = await carded(_reply(_item()))
    capture._drafts[(card.chat.id, card.message_id)].family_id += 1

    call = await _tap("save", card, session, family, anya, bot)
    assert call.alert == texts.CAPTURE_ALIEN
    assert await _entries(session, family) == []
