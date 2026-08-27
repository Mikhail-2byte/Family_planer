"""Пошаговый мастер `/new` — путь создания записи полностью без LLM.

Нужен как запасной вход: когда OpenRouter недоступен или разобрал текст неверно,
записать что-то в семью всё равно можно (PLAN.md, «Разбор естественного текста»).
"""

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

CANCEL = "new:cancel"


class New(StatesGroup):
    kind = State()
    title = State()
    day = State()
    at = State()
    remind = State()


def _rows(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


CANCEL_KB = _rows([("❌ Отмена", CANCEL)])

KIND_KB = _rows(
    [("✅ Задача", "new:kind:task"), ("📝 Заметка", "new:kind:note")],
    [("📅 Событие", "new:kind:event"), ("🛒 Покупка", "new:kind:shopping")],
    [("❌ Отмена", CANCEL)],
)

DAY_KB = _rows(
    [("Сегодня", "new:day:0"), ("Завтра", "new:day:1"), ("Послезавтра", "new:day:2")],
    [("Другая дата", "new:day:other"), ("Без даты", "new:day:none")],
    [("❌ Отмена", CANCEL)],
)

TIME_KB = _rows(
    [("09:00", "new:at:09:00"), ("12:00", "new:at:12:00"), ("19:00", "new:at:19:00")],
    [("Другое время", "new:at:other"), ("Весь день", "new:at:allday")],
    [("❌ Отмена", CANCEL)],
)

REMIND_KB = _rows(
    [("За 15 минут", "new:rem:15"), ("За час", "new:rem:60")],
    [("В момент", "new:rem:0"), ("Без напоминания", "new:rem:none")],
    [("❌ Отмена", CANCEL)],
)

ASK_KIND = "Что записываем?"
ASK_TITLE = "Что записать? Напишите одним сообщением."
ASK_DAY = "На какой день?"
ASK_DAY_MANUAL = "Введите дату: <code>27.08</code> или <code>27.08.2026</code>"
ASK_TIME = "Во сколько?"
ASK_TIME_MANUAL = "Введите время: <code>19:30</code>"
ASK_REMIND = "Напомнить заранее?"
CANCELLED = "Отменено."
NOT_YOURS = "Это чужая запись — нажмите /new, чтобы начать свою."
BAD_DATE = "Не понял дату. Нужен формат <code>27.08</code> или <code>27.08.2026</code>."
BAD_TIME = "Не понял время. Нужен формат <code>19:30</code>."


@router.message(Command("new"))
@router.message(F.text == kb.BTN_NEW)
async def start_wizard(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(New.kind)
    await message.answer(ASK_KIND, reply_markup=KIND_KB)


# Зарегистрирован до шагов мастера, иначе `/cancel` на шаге «текст» уедет в
# заголовок записи. Цена — записать заметку со словом «/cancel» нельзя
@router.message(Command("cancel"), StateFilter(New))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(CANCELLED)


@router.callback_query(StateFilter(New), F.data == CANCEL)
async def cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(CANCELLED)
    await call.answer()


@router.callback_query(New.kind, F.data.startswith("new:kind:"))
async def pick_kind(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(kind=call.data.split(":")[-1])
    await state.set_state(New.title)
    await call.message.edit_text(ASK_TITLE, reply_markup=CANCEL_KB)
    await call.answer()


@router.message(New.title, F.text)
async def take_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(New.day)
    await message.answer(ASK_DAY, reply_markup=DAY_KB)


@router.callback_query(New.day, F.data.startswith("new:day:"))
async def pick_day(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, family: Family,
    member: Member,
) -> None:
    choice = call.data.split(":")[-1]
    if choice == "other":
        await call.message.edit_text(ASK_DAY_MANUAL, reply_markup=CANCEL_KB)
        await call.answer()
        return
    if choice == "none":
        await call.message.delete()
        await _save(call.message, state, session, family, member)
        await call.answer()
        return

    day = tu.local_today(family.tz) + timedelta(days=int(choice))
    await state.update_data(day=day.isoformat())
    await state.set_state(New.at)
    await call.message.edit_text(ASK_TIME, reply_markup=TIME_KB)
    await call.answer()


@router.message(New.day, F.text)
async def take_day(message: Message, state: FSMContext, family: Family) -> None:
    day = _parse_day(message.text, tu.local_today(family.tz))
    if day is None:
        await message.answer(BAD_DATE)
        return
    await state.update_data(day=day.isoformat())
    await state.set_state(New.at)
    await message.answer(ASK_TIME, reply_markup=TIME_KB)


@router.callback_query(New.at, F.data.startswith("new:at:"))
async def pick_time(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, family: Family,
    member: Member,
) -> None:
    choice = call.data.removeprefix("new:at:")
    if choice == "other":
        await call.message.edit_text(ASK_TIME_MANUAL, reply_markup=CANCEL_KB)
        await call.answer()
        return
    if choice == "allday":
        await state.update_data(all_day=True)
        await call.message.delete()
        await _save(call.message, state, session, family, member)
        await call.answer()
        return

    await state.update_data(at=choice)
    await state.set_state(New.remind)
    await call.message.edit_text(ASK_REMIND, reply_markup=REMIND_KB)
    await call.answer()


@router.message(New.at, F.text)
async def take_time(message: Message, state: FSMContext) -> None:
    try:
        moment = tu.parse_hhmm(message.text)
    except ValueError:
        await message.answer(BAD_TIME)
        return
    await state.update_data(at=f"{moment:%H:%M}")
    await state.set_state(New.remind)
    await message.answer(ASK_REMIND, reply_markup=REMIND_KB)


@router.callback_query(New.remind, F.data.startswith("new:rem:"))
async def pick_remind(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, family: Family,
    member: Member,
) -> None:
    choice = call.data.split(":")[-1]
    if choice != "none":
        await state.update_data(remind_before=int(choice))
    await call.message.delete()
    await _save(call.message, state, session, family, member)
    await call.answer()


@router.message(StateFilter(New), F.text)
async def unexpected_text(message: Message) -> None:
    """Пока идёт мастер, обычные реплики не должны молча теряться."""
    await message.answer(
        "Сейчас идёт запись через /new — выберите вариант кнопкой "
        "или наберите /cancel."
    )


# Ниже всех обработчиков мастера: сюда попадают только тапы, которые не подошли
# ни одному состоянию — чужая карточка или карточка, оставшаяся от прошлой
# сессии. Без ответа у нажавшего до таймаута крутится «часик»
@router.callback_query(F.data.startswith("new:"))
async def stale_tap(call: CallbackQuery) -> None:
    await call.answer(NOT_YOURS, show_alert=True)


def _parse_day(raw: str, today):
    """'27.08' или '27.08.2026'. Без года берём ближайший будущий."""
    parts = raw.strip().replace("/", ".").split(".")
    try:
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) > 2 else today.year
        if len(parts) > 2 and year < 100:
            year += 2000
        parsed = datetime(year, month, day).date()
        if len(parts) == 2 and parsed < today:
            # 29.02 в високосном году переносится в невисокосный — это ValueError
            parsed = parsed.replace(year=year + 1)
    except (ValueError, IndexError):
        return None
    return parsed


async def _save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    family: Family,
    member: Member,
) -> None:
    data = await state.get_data()
    await state.clear()

    due_at = None
    all_day = bool(data.get("all_day"))
    if data.get("day"):
        day = datetime.fromisoformat(data["day"]).date()
        moment = tu.parse_hhmm(data["at"]) if data.get("at") else datetime.min.time()
        all_day = all_day or not data.get("at")
        due_at = tu.to_utc(datetime.combine(day, moment), family.tz)

    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=member.id,
        kind=data.get("kind", "task"),
        title=data.get("title", "Без названия"),
        due_at=due_at,
        all_day=all_day,
        source_chat_id=message.chat.id,
    )

    before = data.get("remind_before")
    if due_at is not None and before is not None:
        await repo.create_reminder(
            session,
            family_id=family.id,
            created_by=member.id,
            text=entry.title,
            fire_at=due_at - timedelta(minutes=before),
            entry_id=entry.id,
        )

    await session.refresh(entry, ["author"])
    await message.answer(
        f"{texts.SAVED}\n\n{texts.entry_card(entry, family.tz)}",
        reply_markup=kb.main_keyboard(),
    )
