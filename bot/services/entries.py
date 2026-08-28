"""Перенос записи на другой день — общий для разбора незакрытого и карточки.

Отдельный модуль появился на этапе 7, когда переносить срок понадобилось из
второго места. Сам перенос — одна строка в `repo`, а вот **гашение разовых
напоминаний** забыть очень легко: без него перенесённая запись выстрелит по
прежнему `fire_at`, то есть догонкой в ближайший тик. Ровно эта поломка и
породила цикл в `handlers/review.py` на этапе 5п; третьей копии быть не должно.

Модуль знает про БД, но не про aiogram: `Family` нужен только ради таймзоны.
"""

from datetime import date, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Entry, Family
from bot.services import timeutil as tu


async def move(
    session: AsyncSession,
    entry: Entry,
    family: Family,
    target: date,
    *,
    all_day: bool | None = None,
) -> Entry | None:
    """Перенести запись на день `target`, сохранив время суток.

    Счёт идёт **через локальное время семьи**: сдвиг `timedelta` поверх UTC
    уехал бы на час при переводе часов, а «завтра в 19:00» обязано остаться в
    19:00.

    У записи без срока времени суток нет вовсе, и брать его неоткуда —
    `to_local(None)` просто упал бы. Такая запись становится «на весь день»:
    срок — локальная полночь, как у всех all-day записей проекта. Вызывающий
    может сказать это явно через `all_day`; `None` означает «оставить как есть».
    """
    if entry.due_at is None:
        moment = time.min
        if all_day is None:
            all_day = True
    else:
        moment = tu.to_local(entry.due_at, family.tz).time()

    due_at = tu.to_utc(datetime.combine(target, moment), family.tz)
    return await _apply(session, entry, family, due_at, all_day)


async def clear_due(
    session: AsyncSession, entry: Entry, family: Family
) -> Entry | None:
    """Снять срок совсем.

    `all_day` обнуляется вместе с ним: флаг «на весь день» без дня — мусор,
    который при следующем переносе увёл бы напоминание в ветку all-day.

    Разовые напоминания гаснут, как и при переносе: срока у записи больше нет,
    а напоминание о нём выстрелило бы по-прежнему.
    """
    return await _apply(session, entry, family, None, False)


async def _apply(
    session: AsyncSession,
    entry: Entry,
    family: Family,
    due_at: datetime | None,
    all_day: bool | None,
) -> Entry | None:
    moved = await repo.reschedule_entry(
        session, entry.id, family.id, due_at, all_day=all_day
    )
    if moved is None:
        return None

    # Старое напоминание выстрелило бы по прежнему `fire_at` — по сути в
    # прошлое, то есть догонкой в ближайший тик. Повторяющиеся не трогаем:
    # тихо убить серию хуже, чем оставить её как есть
    for reminder in await repo.entry_reminders(session, entry.id):
        if reminder.rrule is None:
            await repo.deactivate_reminder(session, reminder)
    return moved
