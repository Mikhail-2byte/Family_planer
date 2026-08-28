"""Разбор незакрытого: список просроченных записей под кнопки (этап 5п).

Отдельный модуль в `services/`, потому что его зовут двое: утренний дайджест
(отправляет сообщение) и `handlers/review` (перерисовывает его после каждого
тапа). Ни БД-логики, ни aiogram здесь нет — только `repo` и `texts`.

Почему это **второе сообщение**, а не кнопки под самой сводкой: текст сводки
собирает `digest.build_day` — единственное место, где день превращается в
текст, — и его же читают `/today` и закреплённая панель. Нумерация строк, без
которой кнопки не привязать к записям, протекла бы в оба, а у панели кнопок
нет и не будет.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family
from bot.services import timeutil as tu


async def overdue(
    session: AsyncSession, family: Family, now: datetime | None = None
) -> list[Entry]:
    """Всё, что уже просрочено на начало сегодняшних локальных суток.

    Тот же вызов, что и в `digest.build_day`, — списки в сводке и в разборе
    обязаны совпадать, иначе человек увидит в сводке запись, которой нет в
    разборе, и решит, что кнопка пропала.
    """
    moment = now or tu.now_utc()
    start, _ = tu.day_bounds(tu.local_today(family.tz, moment), family.tz)
    return await repo.overdue_entries(session, family.id, start)


def render(entries: list[Entry], tz: str, now: datetime | None = None) -> tuple[str, list[Entry]]:
    """Текст разбора и записи, попавшие в него.

    Возвращает именно показанные записи, а не все: клавиатуру строят по ним, и
    кнопок обязано быть ровно столько, сколько пронумерованных строк — иначе
    номер на кнопке уведёт не на ту запись.

    Потолок двойной. По числу записей — `MAX_REVIEW_ITEMS`, чтобы кнопок не
    стало неприлично много. По длине — на глаз не видно, но `Entry.title` это
    500 символов, и восьми длинных заголовков хватит, чтобы перерасти 4096:
    Telegram ответил бы отказом, то есть сообщение пропало бы целиком.
    """
    if not entries:
        return texts.REVIEW_ALL_CLEAR, []

    head = f"{texts.REVIEW_HEADER}\n"
    foot = f"\n\n{texts.REVIEW_HINT}"
    # Запас на хвост «…и ещё N»: он появится, только если что-то отброшено
    budget = texts.MESSAGE_LIMIT - len(head) - len(foot) - len(texts.review_tail(999))

    lines: list[str] = []
    shown: list[Entry] = []
    for entry in entries[: texts.MAX_REVIEW_ITEMS]:
        line = f"{len(shown) + 1}. {texts.entry_line(entry, tz, now)}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
        shown.append(entry)

    if not shown:
        # Даже одна запись не влезла — заголовок сам по себе бессмыслен
        return texts.REVIEW_ALL_CLEAR, []

    body = head + "\n".join(lines)
    if len(entries) > len(shown):
        body += "\n" + texts.review_tail(len(entries) - len(shown))
    return body + foot, shown
