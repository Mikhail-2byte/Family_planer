"""Просмотр записей: /today /week /tasks /notes /find /family."""

from datetime import date, timedelta
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
from bot.services import digest
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

PAGE_SIZE = 8


def _by_day(entries: list[Entry], tz: str) -> list[tuple[date, list[Entry]]]:
    """Записи → дни по возрастанию, внутри дня порядок из запроса сохранён.

    Выборка приходит отсортированной как «сначала все записи на весь день,
    потом остальные по времени», а `groupby` склеивает только соседние элементы.
    Без пересортировки по дню неделя выводится вперемешку и один и тот же день
    попадает в вывод дважды. `sorted` стабильна, поэтому внутридневной порядок
    не портится.
    """

    def local_day(entry: Entry) -> date:
        return tu.to_local(entry.due_at, tz).date()

    return [
        (day, list(group))
        for day, group in groupby(sorted(entries, key=local_day), key=local_day)
    ]


@router.message(Command("today"))
@router.message(F.text == kb.BTN_TODAY)
async def cmd_today(message: Message, session: AsyncSession, family: Family) -> None:
    # Тот же сборщик, что и у утреннего дайджеста: иначе два вывода одного и
    # того же дня разойдутся при первой же правке формата
    text, _ = await digest.build_day(session, family)
    await message.answer(text, reply_markup=kb.main_keyboard())


@router.message(Command("week"))
async def cmd_week(message: Message, session: AsyncSession, family: Family) -> None:
    now = tu.now_utc()
    today = tu.local_today(family.tz, now)
    start, end = tu.week_bounds(today, family.tz)
    entries = await repo.entries_for_range(session, family.id, start, end)

    if not entries:
        await message.answer(texts.EMPTY_WEEK)
        return

    blocks = [texts.week_header(today - timedelta(days=today.weekday()))]
    for day, group in _by_day(entries, family.tz):
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

    if not entries and total:
        # Закрыли последнюю запись на последней странице: сама страница исчезла,
        # но записи остались. Без этого пользователь упирается в «задач нет»
        # вообще без кнопок и не может вернуться назад
        offset = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
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
        await message.answer(texts.search_empty(query))
        return

    now = tu.now_utc()
    blocks = [
        texts.search_header(query, len(found)),
        "\n".join(texts.entry_line(e, family.tz, now) for e in found),
    ]
    if len(found) == repo.SEARCH_LIMIT:
        # Иначе заголовок «Найдено: 20» выглядит как точное число совпадений
        blocks.append(texts.SEARCH_TRUNCATED.format(limit=repo.SEARCH_LIMIT))
    await message.answer("\n".join(blocks))


@router.message(F.text == kb.BTN_BUY)
async def buy_not_ready(message: Message) -> None:
    """Кнопка стоит на клавиатуре с этапа 1, а списки будут на этапе 4."""
    await message.answer(texts.SOON_SHOPPING)


@router.message(Command("family"))
async def cmd_family(message: Message, session: AsyncSession, family: Family) -> None:
    members = await repo.members_of(session, family.id)
    counts = await repo.entry_counts_by_author(session, family.id)
    lines = [
        texts.family_header(family.title or "Семья", family.tz, family.digest_time)
    ]
    lines += [
        texts.family_member(m.display_name, counts.get(m.id, 0)) for m in members
    ]
    await message.answer("\n".join(lines), reply_markup=kb.main_keyboard())
