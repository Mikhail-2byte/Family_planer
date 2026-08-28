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
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User, Voice

from bot import keyboards as kb
from bot import middlewares as mw
from bot import texts
from bot.db import repo
from bot.handlers import capture, routers
from bot.handlers import review as review_handler
from bot.handlers import settings as settings_handler
from bot.handlers import voice as voice_handler
from bot.services import llm, parsing
from bot.services import voice as stt

BOT_ID = 777
CHAT = Chat(id=-1001, type="supergroup", title="Семья")
ANYA = User(id=222, is_bot=False, first_name="Аня")


class OfflineSession(BaseSession):
    """Сессия, которая никуда не ходит: сети в тестах нет."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self.edited: list[str] = []
        self._next_id = 900

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            self.edited.append(method.text)
            return True
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


@pytest.mark.asyncio
async def test_edit_reply_never_reaches_the_model(wired, asked):
    """Ответ с новым текстом карточки — правка, а не новая фраза.

    Порядок внутри роутера `capture`: `edit_field` стоит выше хендлера с
    `IsTrigger`. Стой он ниже — реплай на карточку был бы обращением по всем
    признакам фильтра, и правка ушла бы в модель отдельным запросом.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "create", "items": [{"title": "перехвачено"}]})

    async with mw.Session() as s:
        family = await repo.get_family(s, CHAT.id)
    card_id = 4242
    key = (CHAT.id, card_id)
    capture._drafts[key] = capture.Draft(
        family.id, [parsing.Item(kind="task", title="Молоко")], 1
    )
    capture._pending[key] = "text"
    try:
        card = Message(
            message_id=card_id,
            date=datetime(2026, 8, 27, 12, 0),
            chat=CHAT,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Планировщик"),
            text="Правильно понял?",
        )
        await dp.feed_update(bot, _update("Купить кефир", message_id=7, reply_to=card))

        assert calls == []
        assert capture._drafts[key].items[0].title == "Купить кефир"
    finally:
        capture._drafts.pop(key, None)
        capture._pending.pop(key, None)


@pytest.mark.asyncio
async def test_settings_reply_reaches_its_handler(wired):
    """`/settings` и ответ на него — сквозь диспетчер, а не вызовом напрямую.

    Обычные тесты `/settings` зовут хендлеры сами и потому не видят, разрешит
    ли aiogram фильтр-функцию `_awaits` и подставит ли `session`, `family`,
    `bot` по именам аргументов. Промах здесь роняет бота в рантайме.
    """
    dp, bot, telegram = wired
    await dp.feed_update(bot, _update("/settings", message_id=20))
    card_id = telegram._next_id  # id сообщения с настройками

    settings_handler._pending[(CHAT.id, card_id)] = settings_handler.TZ
    try:
        card = Message(
            message_id=card_id,
            date=datetime(2026, 8, 27, 12, 0),
            chat=CHAT,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Планировщик"),
            text="Настройки",
        )
        await dp.feed_update(
            bot, _update("Asia/Yekaterinburg", message_id=21, reply_to=card)
        )
    finally:
        settings_handler._pending.clear()

    async with mw.Session() as s:
        family = await repo.get_family(s, CHAT.id)
        assert family.tz == "Asia/Yekaterinburg"
    assert texts.SETTINGS_SAVED in telegram.sent[-1]
    assert telegram.edited and "Asia/Yekaterinburg" in telegram.edited[-1]


def _voice_update(message_id: int = 30, duration: int = 5) -> Update:
    return Update(
        update_id=message_id,
        message=Message(
            message_id=message_id,
            date=datetime(2026, 8, 27, 12, 0),
            chat=CHAT,
            from_user=ANYA,
            voice=Voice(
                file_id="voice-1",
                file_unique_id="uniq-1",
                duration=duration,
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _awaiting():
    voice_handler._awaiting.clear()
    yield
    voice_handler._awaiting.clear()


@pytest.mark.asyncio
async def test_voice_without_a_tap_reaches_nobody(wired, asked, monkeypatch):
    """Голосовое без кнопки не стоит ни одного внешнего вызова.

    Тот же критерий, что и у обычной переписки в этапе 3a, только для голоса:
    молчание держится на фильтре `_invited`, а не на ветке внутри хендлера.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "create", "items": [{"title": "перехвачено"}]})
    heard: list[bytes] = []

    async def fake_transcribe(audio, filename="voice.ogg"):
        heard.append(audio)
        return "перехвачено"

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)

    await dp.feed_update(bot, _voice_update())

    assert heard == []
    assert calls == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_tapped_voice_reaches_the_handler(wired, asked, monkeypatch):
    """Заодно проверка резолва аргументов: session, family, member, bot."""
    dp, bot, telegram = wired
    calls = asked({"intent": "chitchat", "items": []})

    async def fake_transcribe(audio, filename="voice.ogg"):
        return "купить молоко завтра к 19"

    async def fake_download(file, destination=None, **kwargs):
        if destination is not None:
            destination.write(b"OggS")
        return destination

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(bot, "download", fake_download)
    monkeypatch.setattr(voice_handler.settings, "stt_key", "gsk-test")

    await dp.feed_update(bot, _update(kb.BTN_VOICE, message_id=29))
    await dp.feed_update(bot, _voice_update())

    assert texts.VOICE_ASK in telegram.sent[0]
    assert "купить молоко завтра к 19" in telegram.sent[1]
    assert calls == ["купить молоко завтра к 19"]  # ушло в тот же разбор


@pytest.mark.asyncio
async def test_review_reply_never_reaches_the_model(wired, asked, monkeypatch):
    """«Другая дата» ответом на разбор — не фраза для модели.

    Реплай на сообщение бота `IsTrigger` считает обращением, и стой роутер
    `review` позади `capture`, «через неделю» уехало бы в LLM отдельным
    запросом. Та же грабля, что закреплена тестом про правку карточки.
    """
    dp, bot, telegram = wired
    calls = asked({"intent": "create", "items": [{"title": "перехвачено"}]})

    async with mw.Session() as s:
        family = await repo.get_family(s, CHAT.id)
        member = await repo.get_or_create_member(s, family.id, ANYA.id, ANYA.first_name)
        entry = await repo.create_entry(
            s,
            family_id=family.id,
            author_id=member.id,
            kind="task",
            title="Позвонить маме",
            due_at=datetime(2026, 8, 20, 16, 0),
        )

    card_id = 4343
    review_handler._pending[(CHAT.id, card_id)] = entry.id
    try:
        card = Message(
            message_id=card_id,
            date=datetime(2026, 8, 27, 12, 0),
            chat=CHAT,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Планировщик"),
            text="Куда перенести?",
        )
        await dp.feed_update(bot, _update("завтра", message_id=8, reply_to=card))

        assert calls == []  # разбор перехватил раньше, модель не звали
    finally:
        review_handler._pending.clear()

    async with mw.Session() as s:
        moved = await repo.get_entry(s, entry.id)
        assert moved.due_at > datetime(2026, 8, 20, 16, 0)
