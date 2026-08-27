"""Просмотр записей: /today /week /tasks /notes /find /family."""

from datetime import timedelta
from itertools import groupby

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

PAGE_SIZE = 8


@router.message(Command("today"))
@router.message(F.text == kb.BTN_TODAY)
async def cmd_today(message: Message, session: AsyncSession, family: Family) -> None:
    now = tu.now_utc()
    today = tu.local_today(family.tz, now)
    start, end = tu.day_bounds(today, family.tz)

    entries = await repo.entries_for_range(session, family.id, start, end)
    overdue = await repo.overdue_entries(session, family.id, start)

    blocks: list[str] = []
    if overdue:
        blocks.append(
            texts.HEADER_OVERDUE
            + "\n"
            + "\n".join(texts.entry_line(e, family.tz, now) for e in overdue)
        )

    body = "\n".join(
        texts.entry_line(e, family.tz, now, show_date=False) for e in entries
    )
    blocks.append(
        texts.day_header(today, family.tz, now)
        + "\n"
        + (body or texts.EMPTY_TODAY)
    )

    await message.answer("\n\n".join(blocks), reply_markup=kb.main_keyboard())


@router.message(Command("week"))
async def cmd_week(message: Message, session: AsyncSession, family: Family) -> None:
    now = tu.now_utc()
    today = tu.local_today(family.tz, now)
    start, end = tu.week_bounds(today, family.tz)
    entries = await repo.entries_for_range(session, family.id, start, end)

    if not entries:
        await message.answer(texts.EMPTY_WEEK)
        return

    monday = today - timedelta(days=today.weekday())
    header = texts.HEADER_WEEK.format(
        start=f"{monday.day} {tu.MONTHS_SHORT[monday.month - 1]}",
        end=(
            f"{(monday + timedelta(days=6)).day} "
            f"{tu.MONTHS_SHORT[(monday + timedelta(days=6)).month - 1]}"
        ),
    )

    def local_day(entry: Entry):
        return tu.to_local(entry.due_at, family.tz).date()

    blocks = [header]
    for day, group in groupby(entries, key=local_day):
        lines = "\n".join(
            texts.entry_line(e, family.tz, now, show_date=False) for e in group
        )
        blocks.append(f"{texts.day_header(day, family.tz, now)}\n{lines}")

    await message.answer("\n\n".join(blocks))


async def _render_page(
    session: AsyncSession, family: Family, view: str, offset: int
) -> tuple[str, object]:
    kind = "task" if view == "tasks" else "note"
    status = "open" if view == "tasks" else None
    entries, total = await repo.entries_by_kind(
        session, family.id, kind, status=status, limit=PAGE_SIZE, offset=offset
    )

    if not entries:
        empty = texts.EMPTY_TASKS if view == "tasks" else texts.EMPTY_NOTES
        return empty, None

    now = tu.now_utc()
    header = (texts.HEADER_TASKS if view == "tasks" else texts.HEADER_NOTES).format(
        shown=f"{offset + 1}–{offset + len(entries)}", total=total
    )
    numbered = "\n".join(
        f"{i}. {texts.entry_line(e, family.tz, now)}"
        for i, e in enumerate(entries, start=1)
    )
    markup = kb.entry_list_keyboard(entries, view, offset, total, PAGE_SIZE)
    return f"{header}\n{numbered}", markup


@router.message(Command("tasks"))
@router.message(F.text == kb.BTN_TASKS)
async def cmd_tasks(message: Message, session: AsyncSession, family: Family) -> None:
    text, markup = await _render_page(session, family, "tasks", 0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("notes"))
@router.message(F.text == kb.BTN_NOTES)
async def cmd_notes(message: Message, session: AsyncSession, family: Family) -> None:
    text, markup = await _render_page(session, family, "notes", 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.PageCB.filter())
async def turn_page(
    call: CallbackQuery,
    callback_data: kb.PageCB,
    session: AsyncSession,
    family: Family,
) -> None:
    text, markup = await _render_page(
        session, family, callback_data.view, callback_data.offset
    )
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(kb.DoneCB.filter())
async def mark_done(
    call: CallbackQuery,
    callback_data: kb.DoneCB,
    session: AsyncSession,
    family: Family,
    member: Member,
) -> None:
    entry = await repo.complete_entry(
        session, callback_data.entry_id, family.id, member.id
    )
    if entry is None:
        await call.answer(texts.DONE_ALREADY, show_alert=True)
        return

    await call.answer(texts.DONE_CONFIRMED.format(title=entry.title[:60]))
    text, markup = await _render_page(session, family, "tasks", callback_data.offset)
    await call.message.edit_text(text, reply_markup=markup)


@router.message(Command("find"))
async def cmd_find(
    message: Message, command: CommandObject, session: AsyncSession, family: Family
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(texts.FIND_USAGE)
        return

    found = await repo.search_entries(session, family.id, query)
    if not found:
        await message.answer(texts.EMPTY_SEARCH.format(query=query))
        return

    now = tu.now_utc()
    lines = "\n".join(texts.entry_line(e, family.tz, now) for e in found)
    header = texts.HEADER_SEARCH.format(query=query, count=len(found))
    await message.answer(f"{header}\n{lines}")


@router.message(Command("family"))
async def cmd_family(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    counts = await repo.entry_counts_by_author(session, family.id)
    lines = [
        texts.FAMILY_HEADER.format(
            title=family.title or "Семья", tz=family.tz, digest=family.digest_time
        )
    ]
    lines += [
        texts.FAMILY_MEMBER.format(name=m.display_name, count=counts.get(m.id, 0))
        for m in members
    ]
    await message.answer("\n".join(lines), reply_markup=kb.main_keyboard())
