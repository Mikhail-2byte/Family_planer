"""Утренняя сводка дня.

Собирается из того же материала, что и `/today`: просрочка, записи на сегодня.
`build_day` — единственное место, где день превращается в текст; `views.cmd_today`
зовёт её же, иначе два вывода одного и того же разойдутся при первой правке.
"""

import logging
from datetime import date, datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Family
from bot.services import sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)


async def build_day(
    session: AsyncSession, family: Family, now: datetime | None = None
) -> tuple[str, bool]:
    """Текст дня и признак «есть что показывать».

    Признак нужен дайджесту: рассылать каждое утро «на сегодня ничего не
    запланировано» незачем. Команде `/today` он не нужен — там пустой ответ
    как раз уместен.
    """
    moment = now or tu.now_utc()
    today = tu.local_today(family.tz, moment)
    start, end = tu.day_bounds(today, family.tz)

    entries = await repo.entries_for_range(session, family.id, start, end)
    overdue = await repo.overdue_entries(session, family.id, start)

    blocks: list[str] = []
    if overdue:
        blocks.append(
            texts.HEADER_OVERDUE
            + "\n"
            + "\n".join(texts.entry_line(e, family.tz, moment) for e in overdue)
        )

    body = "\n".join(
        texts.entry_line(e, family.tz, moment, show_date=False) for e in entries
    )
    blocks.append(
        texts.day_header(today, family.tz, moment) + "\n" + (body or texts.EMPTY_TODAY)
    )
    return "\n\n".join(blocks), bool(entries or overdue)


def is_due(family: Family, now: datetime) -> bool:
    """Пора ли слать дайджест: сегодня ещё не слали и время наступило."""
    today = tu.local_today(family.tz, now)
    if family.last_digest_on is not None and family.last_digest_on >= today:
        return False
    try:
        moment = tu.parse_hhmm(family.digest_time)
    except (ValueError, AttributeError):
        # Кривая строка в БД не должна останавливать дайджест остальным семьям
        log.warning("Семья #%s: непонятное digest_time %r", family.id, family.digest_time)
        return False
    return now >= tu.at_local_time(today, moment, family.tz)


async def send_pending(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Разослать дайджест тем семьям, кому пора."""
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        if not is_due(family, moment):
            continue
        try:
            await _send_one(bot, session, family, moment)
        except Exception:
            log.exception("Дайджест семьи #%s не отправлен", family.id)


async def _send_one(
    bot: Bot, session: AsyncSession, family: Family, now: datetime
) -> None:
    today = tu.local_today(family.tz, now)
    text, has_content = await build_day(session, family, now)

    if has_content:
        blocks = [texts.DIGEST_HEADER]
        if _is_late(family, today, now):
            blocks.append(texts.DIGEST_LATE_NOTE)
        blocks.append(text)
        if await sending.deliver(bot, family, "\n\n".join(blocks)) == sending.RETRY:
            return  # сеть или флуд — попробуем на следующем тике

    # Отметка ставится и на пустом дне: иначе каждый тик до полуночи будет
    # заново собирать сводку, которую всё равно не отправит
    await repo.set_last_digest_on(session, family, today)


def _is_late(family: Family, today: date, now: datetime) -> bool:
    planned = tu.at_local_time(today, tu.parse_hhmm(family.digest_time), family.tz)
    return (now - planned).total_seconds() > settings.late_silent_min * 60
