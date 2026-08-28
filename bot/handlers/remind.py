"""`/remind <текст>` — напоминание без разбора типа записи.

Дату тянет `bot/services/nlp_fallback.py` на `dateparser`; повторяемость он не
понимает и честно отказывается — до этапа 3a её задаёт мастер `/new`.
"""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Family, Member
from bot.filters import IN_GROUP
from bot.services import nlp_fallback as nlp
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)


@router.message(Command("remind"))
async def cmd_remind(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    family: Family,
    member: Member,
) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(texts.REMIND_USAGE)
        return

    if nlp.looks_recurring(raw):
        await message.answer(texts.REMIND_RECURRING)
        return

    now = tu.now_utc()
    parsed = nlp.parse_when(raw, tu.to_local(now, family.tz))
    if parsed is None:
        await message.answer(texts.REMIND_NO_DATE)
        return
    if not parsed.text:
        await message.answer(texts.REMIND_NO_TEXT)
        return

    fire_at = tu.to_utc(parsed.when, family.tz)
    if fire_at <= now:
        # Иначе тикер честно отработает догонкой и выстрелит немедленно —
        # сюрприз на пустом месте
        await message.answer(texts.remind_in_past(fire_at, family.tz, now))
        return

    await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=member.id,
        text=parsed.text,
        fire_at=fire_at,
    )
    await message.answer(texts.remind_saved(parsed.text, fire_at, family.tz, now))
