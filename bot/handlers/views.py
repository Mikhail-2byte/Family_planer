"""Просмотр записей: /today /week /tasks /notes /events /find /family."""

from datetime import date, timedelta
from itertools import groupby

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import digest, panel
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

PAGE_SIZE = 8


async def edit_or_ignore(
    call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Перерисовать сообщение, стерпев «message is not modified».

    Двое могут тапнуть одну и ту же кнопку на одном сообщении: второй `edit_text`
    получает от Telegram отказ, и без подавления исключение уходит из хендлера —
    у нажавшего до таймаута крутится «часик». Сравнивать надо `exc.message`:
    в тексте исключения есть и метод, и описание.
    """
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message:
            raise


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
        await message.answer(texts.EMPTY_WEEK, reply_markup=kb.main_keyboard())
        return

    # Режем всю неделю целиком, а не каждый день по MAX_DAY_ITEMS: дневной
    # потолок семь раз подряд всё равно перерастает 4096. Срез делаем уже
    # после `_by_day` — выборка отсортирована `all_day DESC, due_at`, и срез
    # сырого списка оставил бы все «весь день» за неделю, обрезав середину,
    # а не хвост
    ordered = [e for _, group in _by_day(entries, family.tz) for e in group]
    shown = ordered[: texts.MAX_WEEK_ITEMS]

    blocks = [texts.week_header(today - timedelta(days=today.weekday()))]
    for day, group in _by_day(shown, family.tz):
        lines = "\n".join(
            texts.entry_line(e, family.tz, now, show_date=False) for e in group
        )
        blocks.append(f"{texts.day_header(day, family.tz, now)}\n{lines}")
    if len(ordered) > len(shown):
        blocks.append(texts.MORE_ITEMS.format(count=len(ordered) - len(shown)))

    # `MAX_WEEK_ITEMS` считает записи, а длину строки задаёт человек: тридцать
    # заголовков по 500 символов перерастают 4096, и Telegram ответит отказом,
    # а не обрезкой. Отбрасываем целыми днями — рвать HTML посередине нельзя
    await message.answer(
        texts.join_under_limit(blocks), reply_markup=kb.main_keyboard()
    )


# Вид страницы ↔ тип записи. Таблицей, а не тернарниками: с приходом `/events`
# ветвлений стало бы по три в каждой из трёх строк
KIND_BY_VIEW = {"tasks": "task", "notes": "note", "events": "event"}
VIEW_BY_KIND = {kind: view for view, kind in KIND_BY_VIEW.items()}
HEADER_BY_VIEW = {
    "tasks": texts.HEADER_TASKS,
    "notes": texts.HEADER_NOTES,
    "events": texts.HEADER_EVENTS,
}
EMPTY_BY_VIEW = {
    "tasks": texts.EMPTY_TASKS,
    "notes": texts.EMPTY_NOTES,
    "events": texts.EMPTY_EVENTS,
}


def view_of(entry: Entry) -> str:
    """На какую страницу возвращать после действия над записью.

    Вид берётся из `entry.kind`, а не из `callback_data`, и завести там поле
    нельзя: у кнопок, уже висящих в чате, `callback_data` вида `done:42:0`.
    Функция общая для `mark_done` и карточки записи — две копии этой карты
    разошлись бы на первом же новом типе.

    Покупка сюда попасть не должна: своей страницы у неё нет, она живёт в
    панели списка. Дефолт `tasks` — чтобы не падать, а показать хоть что-то.
    """
    return VIEW_BY_KIND.get(entry.kind, "tasks")


async def _render_page(
    session: AsyncSession, family: Family, view: str, offset: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    kind = KIND_BY_VIEW.get(view, "task")
    # У заметок статус фильтруется с тех пор, как у них появилась кнопка
    # закрытия: иначе закрытая заметка оставалась бы в списке с мёртвой кнопкой
    # («уже закрыта» на каждый тап) и список бы не разгружался
    status = "open"
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
        return EMPTY_BY_VIEW.get(view, texts.EMPTY_TASKS), None

    now = tu.now_utc()
    header = HEADER_BY_VIEW.get(view, texts.HEADER_TASKS).format(
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
    # Inline и нижняя клавиатура в одном сообщении несовместимы, поэтому нижняя
    # достаётся только пустому списку — где она и нужнее всего: экран без единой
    # кнопки читается как «бот сломался». Подставлять её внутри `_render_page`
    # нельзя: оттуда разметка уходит ещё и в `edit_text` у `turn_page` /
    # `mark_done`, а `editMessageText` принимает только InlineKeyboardMarkup
    await message.answer(text, reply_markup=markup or kb.main_keyboard())


@router.message(Command("notes"))
@router.message(F.text == kb.BTN_NOTES)
async def cmd_notes(message: Message, session: AsyncSession, family: Family) -> None:
    text, markup = await _render_page(session, family, "notes", 0)
    await message.answer(text, reply_markup=markup or kb.main_keyboard())


@router.message(Command("events"))
async def cmd_events(message: Message, session: AsyncSession, family: Family) -> None:
    """События отдельной страницей (этап 7).

    До неё событие было видно только в `/today`, `/week` и «Просрочено», а
    поставить кнопки туда нельзя: день собирает `digest.build_day`, и нумерация
    протекла бы в утреннюю сводку и в закреплённую панель. Из-за этого закрыть
    непросроченное событие было нечем — кнопка «Готово» живёт только на такой
    странице.

    Кнопки в нижнюю клавиатуру не добавляем: там уже шесть подписей в трёх
    рядах, седьмая ломает раскладку на телефоне.
    """
    text, markup = await _render_page(session, family, "events", 0)
    await message.answer(text, reply_markup=markup or kb.main_keyboard())


@router.callback_query(kb.PageCB.filter())
async def turn_page(
    call: CallbackQuery,
    callback_data: kb.PageCB,
    session: AsyncSession,
    family: Family,
) -> None:
    # Ответ первым: если перерисовка упрётся в ошибку, у человека всё равно
    # не должен остаться крутящийся индикатор
    await call.answer()
    text, markup = await _render_page(
        session, family, callback_data.view, callback_data.offset
    )
    await edit_or_ignore(call, text, markup)


@router.callback_query(kb.DoneCB.filter())
async def mark_done(
    call: CallbackQuery,
    callback_data: kb.DoneCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    entry = await repo.complete_entry(
        session, callback_data.entry_id, family.id, member.id
    )
    if entry is None:
        await call.answer(texts.DONE_ALREADY, show_alert=True)
        return

    # Вид берём из типа закрытой записи, а не из `callback_data` — почему
    # именно так, сказано в `view_of`. С приходом `/events` тернарник здесь
    # перерисовывал бы страницу событий как страницу задач
    view = view_of(entry)
    await call.answer(
        (texts.NOTE_CLOSED if view == "notes" else texts.DONE_CONFIRMED).format(
            title=entry.title[:60]
        )
    )
    # Раньше перерисовки списка: запись уже закрыта в базе, и сбой рендера
    # страницы не должен оставить панель со сделанной задачей
    panel.schedule(bot, family.id, call.message.message_id)
    text, markup = await _render_page(session, family, view, callback_data.offset)
    await edit_or_ignore(call, text, markup)


@router.message(Command("find"))
async def cmd_find(
    message: Message, command: CommandObject, session: AsyncSession, family: Family
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(texts.FIND_USAGE, reply_markup=kb.main_keyboard())
        return

    found = await repo.search_entries(session, family.id, query)
    if not found:
        await message.answer(
            texts.search_empty(query), reply_markup=kb.main_keyboard()
        )
        return

    now = tu.now_utc()
    header = texts.search_header(query, len(found))
    footer = (
        # Иначе заголовок «Найдено: 20» выглядит как точное число совпадений
        texts.SEARCH_TRUNCATED.format(limit=repo.SEARCH_LIMIT)
        if len(found) == repo.SEARCH_LIMIT
        else ""
    )
    # `SEARCH_LIMIT` считает записи, а не символы: двадцать заголовков по 500
    # символов дают 10 000 и отказ Telegram, то есть потерю всей выдачи.
    # Бюджет — то, что остаётся после заголовка и хвоста; оба считаются до
    # обрезки, иначе хвост может сам не поместиться
    spent = len(header) + len(footer) + 2  # два перевода строки между блоками
    lines = texts.entry_lines(
        found,
        family.tz,
        now,
        limit=repo.SEARCH_LIMIT,
        budget=texts.MESSAGE_LIMIT - spent,
    )
    blocks = [header, "\n".join(lines)]
    if footer:
        blocks.append(footer)
    await message.answer("\n".join(blocks), reply_markup=kb.main_keyboard())


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
