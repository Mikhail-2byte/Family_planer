"""`/settings` — таймзона и время утренней сводки (шаг 3b.6)."""

from types import SimpleNamespace

import pytest

from bot import texts
from bot.handlers import settings as handler


class FakeMessage:
    def __init__(self, chat_id: int = -1001, message_id: int = 300, text: str = ""):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = message_id
        self.text = text
        self.reply_to_message = None
        self.replies: list[str] = []
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=self.message_id * 10, chat=self.chat)

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCall:
    def __init__(self, chat_id: int, message_id: int, data: str):
        self.data = data
        self.message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=chat_id, type="supergroup")
        )
        self.alerts: list[str] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.alerts.append(text)


@pytest.fixture(autouse=True)
def _pending():
    handler._pending.clear()
    yield
    handler._pending.clear()


async def _edit(family, bot, field: str, value: str, card_id: int = 3000):
    """Тап по кнопке и ответ реплаем — тот же путь, что у карточки разбора."""
    call = FakeCall(family.chat_id, card_id, field)
    await handler.ask(call)

    reply = FakeMessage(chat_id=family.chat_id, message_id=card_id + 1, text=value)
    reply.reply_to_message = SimpleNamespace(message_id=card_id)
    assert handler._awaits(reply)
    return call, reply


@pytest.mark.asyncio
async def test_settings_shows_current_values(family):
    message = FakeMessage(chat_id=family.chat_id)
    await handler.cmd_settings(message, family)

    text, kwargs = message.sent[0]
    assert family.tz in text
    assert family.digest_time in text
    assert kwargs["reply_markup"] is handler.KEYBOARD


@pytest.mark.asyncio
async def test_settings_does_not_promise_a_listening_mode(family):
    """Сторож против возврата отменённого режима `all` (3b.7).

    Режим отменён 28.08.2026, а колонка `families.listen_mode` осталась в базе
    мёртвой — соблазн «раз поле есть, покажем его» никуда не делся. Показанная
    настройка, которую нельзя изменить, хуже отсутствующей: она обещает
    переключатель. Но на что бот отзывается, экран сказать обязан.
    """
    message = FakeMessage(chat_id=family.chat_id)
    await handler.cmd_settings(message, family)

    text, _ = message.sent[0]
    assert "Режим прослушивания" not in text
    assert texts.SETTINGS_MODE_NOTE in text


@pytest.mark.asyncio
async def test_timezone_change_reaches_the_database(session, family, bot):
    _, reply = await _edit(family, bot, handler.TZ, "Asia/Yekaterinburg")
    await handler.take_value(reply, session, family, bot)

    assert family.tz == "Asia/Yekaterinburg"
    assert reply.replies == [texts.SETTINGS_SAVED]
    # Экран настроек перерисован, а не отправлен заново
    assert bot.edited and "Asia/Yekaterinburg" in bot.edited[-1][2]


@pytest.mark.asyncio
async def test_unknown_timezone_is_refused(session, family, bot):
    """Битая зона в базе сломала бы разом тикер, дайджест, панель и рендер дат."""
    before = family.tz
    _, reply = await _edit(family, bot, handler.TZ, "Марс/Олимп")
    await handler.take_value(reply, session, family, bot)

    assert family.tz == before
    assert reply.replies == [texts.SETTINGS_BAD_TZ]
    # Ожидание не снято: человек может ответить ещё раз, не нажимая кнопку
    assert handler._pending[(family.chat_id, 3000)] == handler.TZ


@pytest.mark.parametrize(
    "raw",
    [
        "Europe/\nMoscow",  # ZoneInfo бросает на этом OSError мимо except
        "Europe\\Moscow",  # а это он на Windows молча принимает
    ],
)
@pytest.mark.asyncio
async def test_broken_timezone_never_crashes_the_handler(session, family, bot, raw):
    """Упавший хендлер = апдейт, потерянный навсегда (инвариант проекта).

    Плюс `Europe\\Moscow` прошёл бы валидацию на Windows и сломал бы рендер
    каждой даты на Linux-VPS — проверять надо по списку зон, а не конструктором.
    """
    before = family.tz
    _, reply = await _edit(family, bot, handler.TZ, raw)
    await handler.take_value(reply, session, family, bot)

    assert family.tz == before
    assert reply.replies == [texts.SETTINGS_BAD_TZ]


@pytest.mark.asyncio
async def test_digest_time_change_reaches_the_database(session, family, bot):
    _, reply = await _edit(family, bot, handler.DIGEST, "7:5")
    await handler.take_value(reply, session, family, bot)

    # Время нормализуется: `digest_time` — колонка на пять символов, и
    # `digest.py` разбирает её строгим `parse_hhmm`
    assert family.digest_time == "07:05"


@pytest.mark.asyncio
async def test_bad_time_is_refused(session, family, bot):
    before = family.digest_time
    _, reply = await _edit(family, bot, handler.DIGEST, "утром")
    await handler.take_value(reply, session, family, bot)

    assert family.digest_time == before
    assert reply.replies == [texts.SETTINGS_BAD_TIME]


@pytest.mark.asyncio
async def test_reply_to_a_stranger_message_is_not_ours(family):
    """Фильтр обязан быть точным: он стоит раньше мастера и разбора."""
    reply = FakeMessage(chat_id=family.chat_id, text="Europe/Moscow")
    reply.reply_to_message = SimpleNamespace(message_id=999)
    assert handler._awaits(reply) is False
