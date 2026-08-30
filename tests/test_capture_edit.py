"""Этап 3b: запасной разбор, дата в прошлом, правка карточки кнопками.

Отдельно от `test_capture.py`, где живут проверки самого разбора (3a): там
модель всегда отвечает, а здесь половина тестов — про то, что бывает, когда она
не отвечает вовсе.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db.models import Entry
from bot.handlers import capture
from bot.services import llm
from bot.services import timeutil as tu

MSK = "Europe/Moscow"


# --- заглушки -----------------------------------------------------------------


class FakeCard:
    """Сообщение бота с карточкой: его правят, а не шлют заново."""

    def __init__(self, message_id: int, chat_id: int):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text: str, reply_markup=None, **kwargs) -> None:
        self.edits.append((text, reply_markup))


class FakeMessage:
    def __init__(self, chat_id: int = -1001, message_id: int = 500, text: str = ""):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = message_id
        self.text = text
        self.reply_to_message = None
        self.replies: list[str] = []
        self.cards: list[FakeCard] = []
        self._next_id = message_id * 10

    async def answer(self, text: str, **kwargs) -> FakeCard:
        self._next_id += 1
        self.replies.append(text)
        card = FakeCard(self._next_id, self.chat.id)
        # Карточкой считается только сообщение с кнопками: отказ — это тоже
        # ответ, но черновика за ним нет
        if kwargs.get("reply_markup") is not None:
            self.cards.append(card)
        return card

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCall:
    def __init__(self, message: FakeCard):
        self.message = message
        self.answers: list[tuple[str | None, dict]] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append((text, kwargs))


@pytest.fixture(autouse=True)
def _state():
    """Черновики и ожидания правки живут в модульных словарях."""
    capture._drafts.clear()
    capture._pending.clear()
    yield
    capture._drafts.clear()
    capture._pending.clear()


@pytest.fixture
def no_model(monkeypatch):
    """OpenRouter недоступен: и сеть, и неверный ключ дают одинаковый `None`."""

    async def fake_ask(system, user, **kwargs):
        return None

    monkeypatch.setattr(llm, "ask", fake_ask)


@pytest.fixture
def model(monkeypatch):
    def use(reply):
        async def fake_ask(system, user, **kwargs):
            return reply

        monkeypatch.setattr(llm, "ask", fake_ask)

    return use


@pytest_asyncio.fixture
async def run(session, family, anya, bot):
    """Прогон фразы до карточки."""

    async def go(text: str, message_id: int = 500):
        message = FakeMessage(chat_id=family.chat_id, message_id=message_id, text=text)
        await capture.capture(message, text, session, family, anya, bot)
        card = message.cards[0] if message.cards else None
        return message, card

    return go


async def _tap(action, card, session, family, member, bot, kind=""):
    call = FakeCall(card)
    await capture.tap(
        call, kb.CaptureCB(action=action, kind=kind), session, family, member, bot
    )
    return call


async def _answer_card(card, text, family, bot, chat_id=None):
    """Человек отвечает реплаем на карточку — так правятся дата и текст."""
    message = FakeMessage(chat_id=chat_id or card.chat.id, message_id=901, text=text)
    message.reply_to_message = card
    assert capture._awaits_edit(message), "карточка не ждёт ответа"
    await capture.edit_field(message, family, bot)
    return message


def _draft(card):
    return capture._drafts[(card.chat.id, card.message_id)]


# --- 3b.1 запасной разбор -----------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_shows_a_card_when_the_model_is_down(run, no_model):
    """Без OpenRouter бот жив и что-то всё же понимает."""
    message, card = await run("забрать посылку завтра в 19:00")

    assert card is not None
    assert texts.CAPTURE_VIA_FALLBACK in message.replies[0]
    draft = _draft(card)
    assert draft.via == "dateparser"
    item = draft.items[0]
    assert item.title == "забрать посылку"
    assert item.due_at.hour == 19
    # Тип определять нечем — это задача, пока человек не поправит кнопкой
    assert item.kind == "task"


@pytest.mark.asyncio
async def test_fallback_does_not_invent_a_time(run, no_model):
    """«Завтра» без времени — запись на весь день, а не «завтра в 14:37».

    `dateparser` берёт неназванное время из «сейчас», и человек унёс бы эту
    выдумку в базу, подтвердив карточку кнопкой.
    """
    message, card = await run("забрать посылку завтра")

    item = _draft(card).items[0]
    assert item.all_day is True
    assert (item.due_at.hour, item.due_at.minute) == (0, 0)
    assert item.due_at.date() == tu.to_local(tu.now_utc(), MSK).date() + timedelta(
        days=1
    )


@pytest.mark.asyncio
async def test_fallback_keeps_a_stated_time(run, no_model):
    """Обратная половина: названное время обязано дожить до карточки."""
    message, card = await run("забрать посылку завтра в 19:00")

    item = _draft(card).items[0]
    assert item.all_day is False
    assert (item.due_at.hour, item.due_at.minute) == (19, 0)


@pytest.mark.asyncio
async def test_fallback_refuses_recurring_instead_of_guessing(run, no_model):
    """`dateparser` выбросил бы «каждый» и молча сделал повтор разовым."""
    message, card = await run("выносить мусор каждый вторник в 19:00")

    assert card is None
    assert message.replies == [texts.CAPTURE_RECURRING_FALLBACK]
    assert capture._drafts == {}


@pytest.mark.asyncio
async def test_fallback_gives_up_and_offers_the_wizard(run, no_model):
    message, card = await run("абракадабра")

    assert card is None
    assert message.replies == [texts.CAPTURE_FAILED]
    assert capture._drafts == {}


# --- 3b.2 дата в прошлом ------------------------------------------------------


def _past(hours: float = 3) -> str:
    local = tu.to_local(tu.now_utc(), MSK) - timedelta(hours=hours)
    return local.replace(microsecond=0).isoformat()


@pytest.mark.asyncio
async def test_past_date_is_flagged_and_the_button_renamed(run, model):
    """«напомни вчера в 19» — предупреждение, а не выстрел."""
    model(
        {
            "intent": "create",
            "items": [{"kind": "task", "title": "Позвонить маме", "due_at": _past()}],
        }
    )
    message, card = await run("напомни вчера в 19 позвонить маме")

    assert texts.PAST_DATE in message.replies[0]
    _, keyboard = capture._card(_draft(card), MSK)
    assert keyboard.inline_keyboard[1][0].text == kb.BTN_SAVE_ANYWAY


@pytest.mark.asyncio
async def test_all_day_today_is_not_called_past(run, model):
    """«Купить молоко сегодня» сказано днём — срок у записи локальная полночь.

    Посекундное сравнение объявляло бы просроченной самую обычную запись, а
    кнопка становилась бы «Всё равно сохранить» на ровном месте.
    """
    today = tu.to_local(tu.now_utc(), MSK).date()
    model(
        {
            "intent": "create",
            "items": [
                {
                    "kind": "shopping",
                    "title": "Купить молоко",
                    "due_at": today.isoformat(),
                    "all_day": True,
                }
            ],
        }
    )
    message, card = await run("купить молоко сегодня")

    assert texts.PAST_DATE not in message.replies[0]
    _, keyboard = capture._card(_draft(card), MSK)
    assert keyboard.inline_keyboard[1][0].text == kb.BTN_SAVE


@pytest.mark.asyncio
async def test_all_day_yesterday_is_still_past(run, model):
    yesterday = tu.to_local(tu.now_utc(), MSK).date() - timedelta(days=1)
    model(
        {
            "intent": "create",
            "items": [
                {
                    "kind": "shopping",
                    "title": "Купить молоко",
                    "due_at": yesterday.isoformat(),
                    "all_day": True,
                }
            ],
        }
    )
    message, _ = await run("купить молоко вчера")

    assert texts.PAST_DATE in message.replies[0]


@pytest.mark.asyncio
async def test_future_date_keeps_the_plain_button(run, model):
    future = (tu.to_local(tu.now_utc(), MSK) + timedelta(hours=3)).replace(
        microsecond=0
    )
    model(
        {
            "intent": "create",
            "items": [
                {"kind": "task", "title": "Позвонить маме", "due_at": future.isoformat()}
            ],
        }
    )
    message, card = await run("позвонить маме")

    assert texts.PAST_DATE not in message.replies[0]
    _, keyboard = capture._card(_draft(card), MSK)
    assert keyboard.inline_keyboard[1][0].text == kb.BTN_SAVE


# --- 3b.4 правка типа ---------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_tap_keeps_the_draft(run, model, session, family, anya, bot):
    """Главная ловушка правки: в 3a черновик снимался на любом тапе."""
    model({"intent": "create", "items": [{"kind": "task", "title": "Молоко"}]})
    _, card = await run("молоко")

    await _tap("kind", card, session, family, anya, bot)

    assert (card.chat.id, card.message_id) in capture._drafts
    _, markup = card.edits[-1]
    assert markup.inline_keyboard[0][0].text == kb.KIND_BUTTONS[0][0]


@pytest.mark.asyncio
async def test_kind_choice_changes_the_card(run, model, session, family, anya, bot):
    model({"intent": "create", "items": [{"kind": "task", "title": "Молоко"}]})
    _, card = await run("молоко")

    await _tap("kind", card, session, family, anya, bot)
    await _tap("setkind", card, session, family, anya, bot, kind="shopping")

    draft = _draft(card)
    assert draft.items[0].kind == "shopping"
    assert draft.edited is True
    text, markup = card.edits[-1]
    assert texts.KIND_NAMES["shopping"] in text
    # Вернулись к обычным кнопкам, а не остались на выборе типа
    assert markup.inline_keyboard[1][0].text == kb.BTN_SAVE


# --- 3b.3 и 3b.5 правка даты и текста ответом ---------------------------------


@pytest.mark.asyncio
async def test_text_edit_by_reply(run, model, session, family, anya, bot):
    model({"intent": "create", "items": [{"kind": "shopping", "title": "Молоко"}]})
    _, card = await run("молоко")

    await _tap("text", card, session, family, anya, bot)
    assert capture._pending[(card.chat.id, card.message_id)] == "text"

    await _answer_card(card, "Купить кефир", family, bot)

    draft = _draft(card)
    assert draft.items[0].title == "Купить кефир"
    assert draft.edited is True
    # Карточка перерисована на месте, а не отправлена заново
    assert bot.edited and "Купить кефир" in bot.edited[-1][2]
    assert (card.chat.id, card.message_id) not in capture._pending


@pytest.mark.asyncio
async def test_date_edit_by_reply(run, model, session, family, anya, bot):
    model({"intent": "create", "items": [{"kind": "task", "title": "Позвонить маме"}]})
    _, card = await run("позвонить маме")

    await _tap("date", card, session, family, anya, bot)
    await _answer_card(card, "завтра в 19:00", family, bot)

    item = _draft(card).items[0]
    assert item.due_at is not None
    assert (item.due_at.hour, item.due_at.minute) == (19, 0)
    assert item.all_day is False


@pytest.mark.asyncio
async def test_reply_can_clear_the_date(run, model, session, family, anya, bot):
    model(
        {
            "intent": "create",
            "items": [
                {"kind": "task", "title": "Позвонить маме", "due_at": _past()}
            ],
        }
    )
    _, card = await run("позвонить маме")

    await _tap("date", card, session, family, anya, bot)
    await _answer_card(card, "без даты", family, bot)

    item = _draft(card).items[0]
    assert item.due_at is None
    # Заодно ушло предупреждение о прошлом — это тот же путь, что и в карточке
    assert texts.PAST_DATE not in bot.edited[-1][2]


@pytest.mark.asyncio
async def test_unparsable_date_keeps_waiting(run, model, session, family, anya, bot):
    """Иначе человек остаётся с карточкой, которая молча не изменилась."""
    model({"intent": "create", "items": [{"kind": "task", "title": "Позвонить маме"}]})
    _, card = await run("позвонить маме")

    await _tap("date", card, session, family, anya, bot)
    message = await _answer_card(card, "когда-нибудь потом", family, bot)

    assert message.replies == [texts.CAPTURE_BAD_DATE]
    assert capture._pending[(card.chat.id, card.message_id)] == "date"
    assert _draft(card).items[0].due_at is None


@pytest.mark.asyncio
async def test_saved_card_stops_waiting_for_edits(
    run, model, session, family, anya, bot
):
    """После «Сохранить» реплай на карточку — обычная фраза, а не правка."""
    model({"intent": "create", "items": [{"kind": "task", "title": "Молоко"}]})
    _, card = await run("молоко")

    await _tap("text", card, session, family, anya, bot)
    await _tap("save", card, session, family, anya, bot)

    later = FakeMessage(chat_id=card.chat.id, message_id=902, text="Кефир")
    later.reply_to_message = card
    assert capture._awaits_edit(later) is False


# --- 3b.6 автосохранение ------------------------------------------------------


@pytest.mark.asyncio
async def test_autosave_is_off_by_default(run, model, session, family):
    """Инвариант «ничего не сохраняется молча» снимается только флагом в .env."""
    assert settings.autosave_confidence == 0
    model(
        {
            "intent": "create",
            "items": [{"kind": "task", "title": "Молоко", "confidence": 1.0}],
        }
    )
    message, card = await run("молоко")

    assert card is not None
    assert texts.CAPTURE_ASK in message.replies[0]
    assert list(await session.scalars(_select_entries(family.id))) == []


@pytest.mark.asyncio
async def test_autosave_writes_without_a_card(
    run, model, session, family, anya, bot, monkeypatch
):
    monkeypatch.setattr(settings, "autosave_confidence", 0.9)
    model(
        {
            "intent": "create",
            "items": [{"kind": "task", "title": "Молоко", "confidence": 0.95}],
        }
    )
    message, _ = await run("молоко")

    assert texts.SAVED_AUTO in message.replies[0]
    assert capture._drafts == {}
    entries = list(await session.scalars(_select_entries(family.id)))
    assert [e.title for e in entries] == ["Молоко"]


@pytest.mark.asyncio
async def test_autosave_never_takes_a_past_date(
    run, model, session, family, monkeypatch
):
    """Там, где ошибка разбора дороже всего, вопрос задаётся всегда."""
    monkeypatch.setattr(settings, "autosave_confidence", 0.9)
    model(
        {
            "intent": "create",
            "items": [
                {"kind": "task", "title": "Молоко", "confidence": 1.0, "due_at": _past()}
            ],
        }
    )
    message, card = await run("молоко")

    assert card is not None
    assert texts.PAST_DATE in message.replies[0]


def _select_entries(family_id: int):
    from sqlalchemy import select

    return select(Entry).where(Entry.family_id == family_id)
