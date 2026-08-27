"""Запросы к БД. Ничего не знает про aiogram — принимает сессию и данные."""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import sqlite as _sqlite  # noqa: F401  — регистрирует lower_unicode
from bot.db.models import Entry, Family, Member, Reminder
from bot.services.timeutil import now_utc


async def get_family(session: AsyncSession, chat_id: int) -> Family | None:
    return await session.scalar(select(Family).where(Family.chat_id == chat_id))


async def get_or_create_family(
    session: AsyncSession, chat_id: int, title: str | None = None
) -> Family:
    """Семья заводится сама по первому же сообщению — команды `/start` нет."""
    family = await get_family(session, chat_id)
    if family is None:
        family = Family(
            chat_id=chat_id,
            title=title,
            tz=settings.tz_default,
            digest_time=settings.digest_time,
        )
        session.add(family)
        await session.commit()
        await session.refresh(family)
    elif title and family.title != title:
        family.title = title
        await session.commit()
    return family


async def get_or_create_member(
    session: AsyncSession, family_id: int, tg_user_id: int, display_name: str
) -> Member:
    member = await session.scalar(
        select(Member).where(
            Member.family_id == family_id, Member.tg_user_id == tg_user_id
        )
    )
    if member is None:
        member = Member(
            family_id=family_id, tg_user_id=tg_user_id, display_name=display_name
        )
        session.add(member)
        await session.commit()
        await session.refresh(member)
    elif member.display_name != display_name:
        member.display_name = display_name
        await session.commit()
    return member


async def members_of(session: AsyncSession, family_id: int) -> list[Member]:
    result = await session.scalars(
        select(Member).where(Member.family_id == family_id).order_by(Member.joined_at)
    )
    return list(result)


async def create_entry(
    session: AsyncSession,
    *,
    family_id: int,
    author_id: int,
    kind: str,
    title: str,
    body: str | None = None,
    due_at: datetime | None = None,
    all_day: bool = False,
    list_id: int | None = None,
    position: int | None = None,
    assignee_id: int | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> Entry:
    entry = Entry(
        family_id=family_id,
        author_id=author_id,
        kind=kind,
        title=title,
        body=body,
        due_at=due_at,
        all_day=all_day,
        list_id=list_id,
        position=position,
        assignee_id=assignee_id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def get_entry(session: AsyncSession, entry_id: int) -> Entry | None:
    return await session.get(Entry, entry_id)


async def entries_for_range(
    session: AsyncSession,
    family_id: int,
    start_utc: datetime,
    end_utc: datetime,
    *,
    statuses: tuple[str, ...] = ("open", "done"),
) -> list[Entry]:
    """Записи со сроком внутри [start, end). Отсортированы по времени."""
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            Entry.status.in_(statuses),
            Entry.due_at >= start_utc,
            Entry.due_at < end_utc,
        )
        .order_by(Entry.all_day.desc(), Entry.due_at)
    )
    return list(result)


async def overdue_entries(
    session: AsyncSession, family_id: int, before_utc: datetime | None = None
) -> list[Entry]:
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            Entry.status == "open",
            Entry.due_at.is_not(None),
            Entry.due_at < (before_utc or now_utc()),
        )
        .order_by(Entry.due_at)
    )
    return list(result)


async def entries_by_kind(
    session: AsyncSession,
    family_id: int,
    kind: str,
    *,
    status: str | None = "open",
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[Entry], int]:
    """Страница записей одного типа + общее число. Сначала со сроком, затем без."""
    where = [Entry.family_id == family_id, Entry.kind == kind]
    if status:
        where.append(Entry.status == status)

    total = await session.scalar(select(func.count()).select_from(Entry).where(*where))
    result = await session.scalars(
        select(Entry)
        .where(*where)
        .order_by(Entry.due_at.is_(None), Entry.due_at, Entry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result), int(total or 0)


SEARCH_LIMIT = 20


def _like_pattern(query: str) -> str:
    """`%` и `_` в запросе человека — обычные символы, а не подстановки.
    Без экранирования `/find %` находит вообще всё."""
    escaped = query.strip().lower()
    for char in ("\\", "%", "_"):
        escaped = escaped.replace(char, f"\\{char}")
    return f"%{escaped}%"


async def search_entries(
    session: AsyncSession, family_id: int, query: str, limit: int = SEARCH_LIMIT
) -> list[Entry]:
    """Поиск по заголовку и телу, без учёта регистра."""
    pattern = _like_pattern(query)
    result = await session.scalars(
        select(Entry)
        .where(
            Entry.family_id == family_id,
            func.lower_unicode(Entry.title).like(pattern, escape="\\")
            | func.lower_unicode(func.coalesce(Entry.body, "")).like(
                pattern, escape="\\"
            ),
        )
        .order_by(Entry.created_at.desc())
        .limit(limit)
    )
    return list(result)


async def complete_entry(
    session: AsyncSession, entry_id: int, family_id: int, member_id: int
) -> Entry | None:
    """Закрыть запись. `family_id` — чтобы колбэк из чужого чата ничего не тронул."""
    entry = await session.get(Entry, entry_id)
    if entry is None or entry.family_id != family_id or entry.status != "open":
        return None
    entry.status = "done"
    entry.done_at = now_utc()
    entry.done_by = member_id
    await session.commit()
    await session.refresh(entry)
    return entry


async def entry_counts_by_author(
    session: AsyncSession, family_id: int
) -> dict[int, int]:
    rows = await session.execute(
        select(Entry.author_id, func.count())
        .where(Entry.family_id == family_id)
        .group_by(Entry.author_id)
    )
    return {author_id: count for author_id, count in rows}


async def create_reminder(
    session: AsyncSession,
    *,
    family_id: int,
    created_by: int,
    text: str,
    fire_at: datetime,
    entry_id: int | None = None,
    rrule: str | None = None,
) -> Reminder:
    reminder = Reminder(
        family_id=family_id,
        created_by=created_by,
        text=text,
        fire_at=fire_at,
        entry_id=entry_id,
        rrule=rrule,
    )
    session.add(reminder)
    await session.commit()
    await session.refresh(reminder)
    return reminder


async def migrate_family_chat_id(
    session: AsyncSession, old_chat_id: int, new_chat_id: int
) -> bool:
    """Группа стала супергруппой — у неё сменился chat_id (см. PLAN.md, п. 1d).

    Без этого бот просто замолчит: семья привязана к старому chat_id.
    """
    if old_chat_id == new_chat_id:
        return False
    result = await session.execute(
        update(Family)
        .where(Family.chat_id == old_chat_id)
        .values(chat_id=new_chat_id, panel_message_id=None)
    )
    await session.commit()
    return bool(result.rowcount)
