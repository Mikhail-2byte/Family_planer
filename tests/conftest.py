import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import Base
from bot.services import panel


@pytest_asyncio.fixture(autouse=True)
async def _panel_state():
    """Панель держит состояние в модульных словарях — между тестами оно течёт.

    Без уборки один тест видит лок и незавершённый дебаунс другого, а
    оставшаяся задача печатает «Task was destroyed but it is pending».
    """
    yield
    tasks = list(panel._tasks.values())
    panel._tasks.clear()
    panel._locks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


@pytest_asyncio.fixture
async def session_maker():
    """Фабрика сессий на общей БД в памяти.

    Движок для `:memory:` держит StaticPool — одно соединение на всех, поэтому
    разные сессии видят одни и те же данные. Это нужно там, где сессию открывает
    не тест, а сам код (например, `FamilyMiddleware`).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_maker):
    async with session_maker() as s:
        yield s


@pytest_asyncio.fixture
async def family(session):
    fam = await repo.get_or_create_family(session, -1001, "Семья")
    fam.tz = "Europe/Moscow"
    await session.commit()
    return fam


@pytest_asyncio.fixture
async def anya(session, family):
    return await repo.get_or_create_member(session, family.id, 222, "Аня")


class FakeBot:
    """Заглушка вместо `Bot`: складывает отправленное в список.

    Настоящий `Bot` в тестах не создаётся никогда — сети в тестах нет.
    `fail_on` / `fail_on_edit` позволяют проверить, что сбой одной отправки или
    правки не срывает остальные; ключ — порядковый номер вызова.
    """

    def __init__(self, fail_on=None, fail_on_edit=None, fail_on_pin=None):
        self.sent: list[tuple[int, str]] = []
        self.kwargs: list[dict] = []  # чем сопровождалась отправка (reply_markup)
        self.edited: list[tuple[int, int, str]] = []  # chat_id, message_id, текст
        self.pinned: list[int] = []
        self.unpinned: list[int] = []
        self._fail_on = fail_on or {}
        self._fail_on_edit = fail_on_edit or {}
        self._fail_on_pin = fail_on_pin or {}
        # message_id последовательны, как в настоящем чате: на этом держится
        # подсчёт «на сколько сообщений уехала панель»
        self._next_id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs):
        error = self._fail_on.get(len(self.sent))
        self.sent.append((chat_id, text))
        self.kwargs.append(kwargs)
        if error is not None:
            raise error
        self._next_id += 1
        # Раньше здесь был None: `sending.send` берёт отсюда message_id
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text: str, chat_id=None, message_id=None, **kwargs):
        error = self._fail_on_edit.get(len(self.edited))
        self.edited.append((chat_id, message_id, text))
        if error is not None:
            raise error
        return None

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs):
        error = self._fail_on_pin.get(len(self.pinned))
        self.pinned.append(message_id)
        if error is not None:
            raise error
        return None

    async def unpin_chat_message(self, chat_id: int, message_id=None, **kwargs):
        self.unpinned.append(message_id)
        return None

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


@pytest.fixture
def bot():
    return FakeBot()


class FakeMessage:
    """Заглушка вместо `Message` в той части, которой пользуются хендлеры.

    `kwargs` копится по той же причине, что и у `FakeBot`: проверяем, с какой
    клавиатурой ушёл ответ. Свой фейк в `test_wizard.py` остаётся — у карточки
    мастера другой контракт (`edit_text`, `delete`).
    """

    def __init__(self, text: str = "", chat_type: str = "supergroup", chat_id=-1001):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.replies: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return None

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.replies]
