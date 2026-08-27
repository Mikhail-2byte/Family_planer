import logging

from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Family
from bot.filters import IN_GROUP

router = Router()
log = logging.getLogger(__name__)


# Фильтр группы обязателен: тот же переход `kicked → member` прилетает из лички,
# когда человек разблокировал бота, а там middleware не кладёт `family`
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER), IN_GROUP)
async def bot_added(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Бота добавили в группу — здороваемся один раз."""
    await bot.send_message(family.chat_id, texts.GREETING)
    log.info("Добавлен в чат %s (семья #%s)", family.chat_id, family.id)


@router.message(Command("ping"), IN_GROUP)
async def cmd_ping(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    await message.answer(texts.pong(family.title or str(family.chat_id), len(members)))


@router.message(Command("ping"))
async def cmd_ping_private(message: Message) -> None:
    await message.answer(texts.PRIVATE_CHAT)
