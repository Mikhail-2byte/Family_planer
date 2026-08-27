import logging

from aiogram import Bot, F, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter, Command
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Family

router = Router()
log = logging.getLogger(__name__)

IN_GROUP = F.chat.type.in_({"group", "supergroup"})


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(event: ChatMemberUpdated, bot: Bot, family: Family) -> None:
    """Бота добавили в группу — здороваемся один раз."""
    await bot.send_message(family.chat_id, texts.GREETING)
    log.info("Добавлен в чат %s (семья #%s)", family.chat_id, family.id)


@router.message(Command("ping"), IN_GROUP)
async def cmd_ping(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    await message.answer(
        texts.PONG.format(title=family.title or family.chat_id, members=len(members))
    )


@router.message(Command("ping"))
async def cmd_ping_private(message: Message) -> None:
    await message.answer(texts.PRIVATE_CHAT)
