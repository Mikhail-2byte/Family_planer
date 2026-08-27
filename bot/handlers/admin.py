import logging

from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_PRIVATE

router = Router()
log = logging.getLogger(__name__)


# Фильтр группы обязателен: тот же переход `kicked → member` прилетает из лички,
# когда человек разблокировал бота, а там middleware не кладёт `family`
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER), IN_GROUP)
async def bot_added(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Бота добавили в группу — здороваемся один раз."""
    # Клавиатуру прикладываем к первому же сообщению: иначе до первого
    # `/today` её в чате нет вообще, и половина бота выглядит несуществующей
    await bot.send_message(
        family.chat_id, texts.GREETING, reply_markup=kb.main_keyboard()
    )
    log.info("Добавлен в чат %s (семья #%s)", family.chat_id, family.id)


@router.message(Command("ping"), IN_GROUP)
async def cmd_ping(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    await message.answer(texts.pong(family.title or str(family.chat_id), len(members)))


# Последним в модуле и без других фильтров: в личке бот отвечает одно и то же
# на что угодно. Кнопка START шлёт `/start`, а команды-инициализации у бота нет
# (инвариант «Никакого /start»), — без этого хендлера бот в личке просто молчит.
# Ловушка стоит в первом роутере, но соседей не перехватывает: у `views`,
# `remind` и `new_entry` на `message` висит IN_GROUP, в личке они не срабатывают
# в принципе. Потеряет кто-то из них этот фильтр — его хендлеры молча уйдут
# сюда; на этот случай есть тест `test_group_routers_never_run_in_private`.
# Клавиатуру не прикладываем: все её кнопки ведут в групповые хендлеры.
@router.message(IN_PRIVATE)
async def private_chat(message: Message) -> None:
    await message.answer(texts.PRIVATE_CHAT)
