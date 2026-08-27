"""Авторегистрация семьи и участников.

Отдельной команды-инициализации нет: любой апдейт из группового чата заводит
семью и добавляет автора в участники. Это исключает тупик «бота добавили,
никто не нажал /start, ничего не работает» (PLAN.md, п. 1a).

Бот доверяет всем участникам чата — ролей и прав нет (PLAN.md, п. 1b).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Chat, Message, TelegramObject, Update, User

from bot import texts
from bot.db import repo
from bot.db.session import Session

log = logging.getLogger(__name__)

GROUP_CHATS = {"group", "supergroup"}


async def drop_wizard_state(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: Message,
    data: dict[str, Any],
) -> Any:
    """Просмотр или команда посреди `/new` обрывает мастер вслух.

    Роутеры просмотра стоят раньше мастера, поэтому `/today` или кнопка
    нижней клавиатуры выигрывают у шага мастера. Состояние при этом
    оставалось висеть, и следующая реплика человека молча становилась
    заголовком записи — хоть через три дня, таймаута у FSM нет.

    Middleware внутренний: он вызывается, только когда хендлер своего
    роутера уже прошёл фильтры, то есть ровно в момент перехвата.
    Ключ состояния — «чат + пользователь», поэтому реплика второго
    участника чужой мастер не трогает.
    """
    state: FSMContext | None = data.get("state")
    dropped = state is not None and await state.get_state() is not None
    if dropped:
        await state.clear()

    result = await handler(event, data)
    if dropped:
        # Ответ на саму команду важнее — уведомление идёт следом
        await event.answer(texts.WIZARD_DROPPED)
    return result


def _display_name(user: User) -> str:
    return user.full_name or user.username or str(user.id)


def _message_of(event: TelegramObject) -> Message | None:
    """Middleware висит на `dp.update`, поэтому сюда приходит `Update`, а не
    `Message`. Служебные поля миграции лежат внутри вложенного сообщения."""
    if isinstance(event, Update):
        return event.message
    return event if isinstance(event, Message) else None


class FamilyMiddleware(BaseMiddleware):
    """Кладёт в data `session`, `family` и `member`. В личке — только `session`."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat: Chat | None = data.get("event_chat")
        user: User | None = data.get("event_from_user")

        async with Session() as session:
            data["session"] = session

            if chat is None or chat.type not in GROUP_CHATS:
                return await handler(event, data)

            if await self._handle_migration(session, _message_of(event), chat):
                # Чат уехал в супергруппу — под старым chat_id семью не заводим
                return None

            family = await repo.get_or_create_family(session, chat.id, chat.title)
            data["family"] = family

            if user is not None and not user.is_bot:
                data["member"] = await repo.get_or_create_member(
                    session, family.id, user.id, _display_name(user)
                )

            return await handler(event, data)

    @staticmethod
    async def _handle_migration(session, message: Message | None, chat: Chat) -> bool:
        """Возвращает True, если чат перестал существовать под текущим chat_id."""
        if message is None:
            return False
        # Служебных сообщений о переезде приходит два — из старого чата и из
        # нового, — так что второй вызов штатно не делает ничего. Логируем по
        # результату: строка «семья переехала» на несостоявшемся переезде
        # прятала бы как раз ту поломку, которую ловим
        if message.migrate_to_chat_id:
            moved = await repo.migrate_family_chat_id(
                session, chat.id, message.migrate_to_chat_id
            )
            if moved:
                log.info("Семья переехала: %s → %s", chat.id, message.migrate_to_chat_id)
            return True
        if message.migrate_from_chat_id:
            moved = await repo.migrate_family_chat_id(
                session, message.migrate_from_chat_id, chat.id
            )
            if moved:
                log.info("Семья переехала: %s → %s", message.migrate_from_chat_id, chat.id)
        return False
