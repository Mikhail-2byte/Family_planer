"""Сквозной путь через диспетчер: фильтр, middleware, роутеры (шаг 3a.6).

Остальные тесты `capture` зовут хендлеры напрямую и потому не видят целый класс
ошибок: aiogram сам подставляет хендлеру `payload`, `session`, `family` и
`member`, и промах в этих именах роняет бота **в рантайме**, а не на импорте.
Здесь через `dp.feed_update` проходит настоящий `Update` — как из Telegram.

Заодно это единственное место, где проверяется порядок роутеров: `capture`
стоит последним, и обычная переписка обязана не дойти до него вовсе.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from bot import middlewares as mw
from bot import texts
from bot.db import repo
from bot.handlers import routers
from bot.services import llm

BOT_ID = 777
CHAT = Chat(id=-1001, type="supergroup", title="Семья")
ANYA = User(id=222, is_bot=False, first_name="Аня")


class OfflineSession(BaseSession):
    """Сессия, которая никуда не ходит: сети в тестах нет."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self._next_id = 900

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            self.sent.append(method.text)
            self._next_id += 1
            return Message(
                message_id=self._next_id,
                date=datetime(2026, 8, 27, 12, 0),
                chat=CHAT,
                text=method.text,
            )
        raise AssertionError(f"неожиданный вызов Telegram: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


@pytest.fixture(scope="module")
def dispatcher():
    """Один на модуль: роутеры — модульные объекты и к двум диспетчерам не цепляются."""
    dp = Dispatcher()
    dp.update.outer_middleware(mw.FamilyMiddleware())
    dp.include_routers(*routers)
    return dp


@pytest_asyncio.fixture
async def wired(dispatcher, session_maker, monkeypatch):
    """Диспетчер со всеми роутерами и своей БД, но без выхода в сеть."""
    monkeypatch.setattr(mw, "Session", session_maker)
    # Диспетчер живёт весь модуль, а с ним и MemoryStorage: незакрытый мастер
    # одного теста иначе перехватывал бы сообщения следующего
    dispatcher.storage.storage.clear()

    telegram = OfflineSession()
    bot = Bot(token="777:TESTTOKEN", session=telegram)
    # Иначе `IsTrigger` пойдёт за getMe в сеть на первом же сообщении
    bot._me = User(id=BOT_ID, is_bot=True, first_name="Планировщик", username="fam_bot")

    async with session_maker() as s:
        await repo.get_or_create_family(s, CHAT.id, CHAT.title)

    yield dispatcher, bot, telegram
    await bot.session.close()


def _update(text: str, message_id: int = 1, reply_to: Message | None = None) -> Update:
    return Update(
        update_id=message_id,
        message=Message(
            message_id=message_id,
            date=datetime(2026, 8, 27, 12, 0),
            chat=CHAT,
            from_user=ANYA,
            text=text,
            reply_to_message=reply_to,
        ),
    )


@pytest.fixture
def asked(monkeypatch):
    """Считает обращения к модели и отвечает заготовкой."""
    calls: list[str] = []

    def use(reply):
        async def fake_ask(system, user, **kwargs):
            calls.append(user)
            return reply

        monkeypatch.setattr(llm, "ask", fake_ask)
        return calls

    return use


@pytest.mark.asyncio
async def test_plain_talk_never_reaches_the_model(wired, asked):
    """Критерий закрытия этапа 3a: обычная переписка не стоит ни одного вызова.

    Проверяется именно сквозь диспетчер: молчание обеспечивает `IsTrigger`, а не
    ветка `chitchat` — до модели дело не доходит вовсе.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "create", "items": [{"title": "Молоко"}]})

    for phrase in ("а что там с отпуском", "ага", "завтра в 19:00 встречаемся"):
        await dp.feed_update(bot, _update(phrase))

    assert calls == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_trigger_reaches_capture_and_shows_a_card(wired, asked):
    """Заодно проверка резолва аргументов: payload, session, family, member."""
    dp, bot, telegram = wired
    calls = asked(
        {
            "intent": "create",
            "items": [
                {
                    "kind": "shopping",
                    "title": "Купить молоко",
                    "due_at": "2026-08-28T19:00:00",
                    "confidence": 0.95,
                }
            ],
        }
    )

    await dp.feed_update(bot, _update("+купить молоко завтра к 19"))

    assert calls == ["купить молоко завтра к 19"]  # без плюса
    assert len(telegram.sent) == 1
    assert texts.CAPTURE_ASK in telegram.sent[0]
    assert "Купить молоко" in telegram.sent[0]


@pytest.mark.asyncio
async def test_commands_still_win_over_capture(wired, asked):
    """`capture` стоит последним — команда обязана дойти до своего роутера."""
    dp, bot, telegram = wired
    calls = asked(None)

    await dp.feed_update(bot, _update("/today"))

    assert calls == []
    assert len(telegram.sent) == 1
    assert texts.EMPTY_TODAY in telegram.sent[0]


@pytest.mark.asyncio
async def test_wizard_keeps_its_reply(wired, asked):
    """Ответ на вопрос мастера не должен уезжать в разбор.

    Ровно поэтому `capture` стоит позади `new_entry`: обращением считается и
    реплай на сообщение бота, а мастер спрашивает «Что записать?» обычным
    сообщением.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "create", "items": [{"title": "перехвачено"}]})

    await dp.feed_update(bot, _update("/new", message_id=1))
    ask_kind = Message(
        message_id=telegram._next_id,
        date=datetime(2026, 8, 27, 12, 0),
        chat=CHAT,
        from_user=User(id=BOT_ID, is_bot=True, first_name="Планировщик"),
        text=telegram.sent[-1],
    )
    # Человек отвечает реплаем на сообщение бота — для `IsTrigger` это обращение
    await dp.feed_update(bot, _update("Молоко", message_id=2, reply_to=ask_kind))

    assert calls == []  # мастер перехватил раньше, модель не звали


@pytest.mark.asyncio
async def test_reply_to_bot_reaches_capture_without_wizard(wired, asked):
    """Обратная половина предыдущего теста: без мастера тот же реплай — обращение.

    Без неё тот тест был бы вырожденным: он мог бы проходить и потому, что
    реплай на бота вообще ни до чего не доходит.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "chitchat", "items": []})

    from_bot = Message(
        message_id=500,
        date=datetime(2026, 8, 27, 12, 0),
        chat=CHAT,
        from_user=User(id=BOT_ID, is_bot=True, first_name="Планировщик"),
        text="Записал.",
    )
    await dp.feed_update(bot, _update("а ещё хлеба", message_id=2, reply_to=from_bot))

    assert calls == ["а ещё хлеба"]
