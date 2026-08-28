"""Разбор незакрытого: закрыть или перенести (этап 5п).

Утром, следом за сводкой, бот присылает список просроченного с кнопками. Тап
закрывает запись или уводит в перенос: день → напоминание → обратно к списку.

**Состояния почти нет.** Весь шаг переноса едет в `callback_data`: там и
`entry_id`, и выбранный день, и минуты напоминания. Ни FSM, ни словаря
черновиков не нужно — а значит, кнопки переживают перезапуск бота, в отличие
от карточки разбора. Исключение одно: «🗓 Другая дата» отвечают реплаем, и
`_pending` помнит, к какой записи этот ответ, — тот же приём, что в
`capture._pending` и `settings._pending`.

**Роутер обязан стоять раньше мастера `/new`.** Ответ на сообщение бота
`IsTrigger` считает обращением, и стой роутер позади `capture`, «в пятницу»
уехало бы в модель отдельной фразой. Ровно та же причина, по которой раньше
мастера стоит `settings`.

Сам перенос живёт в `services/entries.py`: с этапа 7 он нужен и карточке
записи, а забыть вместе с ним гашение разовых напоминаний слишком легко.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.handlers.new_entry import ALLDAY_ANCHOR
from bot.handlers.views import edit_or_ignore
from bot.services import entries, panel, review, sending
from bot.services import nlp_fallback as nlp
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

log = logging.getLogger(__name__)

# Ключ — (chat_id, message_id) сообщения разбора, значение — какую запись
# переносим. Живёт в памяти и умирает с перезапуском, как и у настроек
_pending: dict[tuple[int, int], int] = {}
MAX_PENDING = 20


def _awaits(message: Message) -> bool:
    """Это ответ на разбор, который ждёт даты?"""
    reply = message.reply_to_message
    return reply is not None and (message.chat.id, reply.message_id) in _pending


async def _live_entry(
    session: AsyncSession, entry_id: int, family: Family
) -> Entry | None:
    """Запись, которую ещё можно разбирать. Изоляция по семье — обязательна."""
    entry = await repo.get_entry(session, entry_id)
    if entry is None or entry.family_id != family.id or entry.status != "open":
        return None
    return entry


async def _redraw(call: CallbackQuery, session: AsyncSession, family: Family) -> None:
    """Перерисовать список: он мог измениться от чужого тапа или от времени."""
    # Не `entries` — это имя занято модулем `services.entries`
    overdue = await review.overdue(session, family)
    text, shown = review.render(overdue, family.tz)
    await edit_or_ignore(call, text, kb.review_keyboard(shown))


def _moved_to(entry: Entry, family: Family, now: datetime) -> str:
    return tu.fmt_due(entry.due_at, family.tz, all_day=entry.all_day, now=now)


@router.callback_query(kb.ReviewCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: kb.ReviewCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """Все кнопки разбора одним хендлером — как `capture.tap`."""
    action = callback_data.action
    entry_id = callback_data.entry_id

    if action == "done":
        entry = await repo.complete_entry(session, entry_id, family.id, member.id)
        if entry is None:
            await call.answer(texts.REVIEW_STALE, show_alert=True)
        else:
            await call.answer(texts.DONE_CONFIRMED.format(title=entry.title[:60]))
            # Раньше перерисовки: запись уже закрыта, и сбой рендера не должен
            # оставить панель дня со сделанным делом
            panel.schedule(bot, family.id, call.message.message_id)
        await _redraw(call, session, family)
        return

    entry = await _live_entry(session, entry_id, family)
    if entry is None:
        await call.answer(texts.REVIEW_STALE, show_alert=True)
        await _redraw(call, session, family)
        return

    if action == "back":
        await call.answer()
        await _redraw(call, session, family)
        return

    if action == "move":
        await call.answer()
        await edit_or_ignore(
            call, texts.review_ask_day(entry.title), kb.review_day_keyboard(entry.id)
        )
        return

    if action == "other":
        key = (call.message.chat.id, call.message.message_id)
        if len(_pending) >= MAX_PENDING and key not in _pending:
            _pending.pop(next(iter(_pending)))
        _pending[key] = entry.id
        await call.answer()
        await edit_or_ignore(
            call,
            f"{texts.review_ask_day(entry.title)}\n\n{texts.REVIEW_ASK_DATE}",
            kb.review_day_keyboard(entry.id),
        )
        return

    if action == "day":
        now = tu.now_utc()
        target = tu.local_today(family.tz, now) + timedelta(days=callback_data.value)
        moved = await entries.move(session, entry, family, target)
        if moved is None:
            await call.answer(texts.REVIEW_STALE, show_alert=True)
            await _redraw(call, session, family)
            return
        await call.answer()
        panel.schedule(bot, family.id, call.message.message_id)
        await edit_or_ignore(
            call,
            texts.REVIEW_ASK_REMIND.format(when=_moved_to(moved, family, now)),
            kb.review_remind_keyboard(moved.id, all_day=moved.all_day),
        )
        return

    # Остались 'rem' и 'norem' — последний шаг переноса
    now = tu.now_utc()
    note = ""
    if action == "rem":
        note = await _remind(session, family, member, entry, callback_data.value, now)
    await call.answer(note or texts.review_moved(entry.title, _moved_to(entry, family, now)))
    await _redraw(call, session, family)


async def _remind(
    session: AsyncSession,
    family: Family,
    member: Member,
    entry: Entry,
    minutes: int,
    now: datetime,
) -> str:
    """Завести напоминание к перенесённой записи. Вернёт оговорку, если не вышло."""
    anchor = entry.due_at
    if entry.all_day:
        # У записи «на весь день» срок — полночь, и отсчитывать «за 15 минут»
        # от неё бессмысленно. Точка отсчёта та же, что у мастера `/new`
        local_day = tu.to_local(entry.due_at, family.tz).date()
        anchor = tu.to_utc(datetime.combine(local_day, ALLDAY_ANCHOR), family.tz)

    fire_at = anchor - timedelta(minutes=minutes)
    if fire_at <= now:
        # Тикер отработал бы такое догонкой и выстрелил в ближайший тик —
        # сюрприз на пустом месте. `/new`, `/remind` и разбор поступают так же
        return texts.remind_in_past(fire_at, family.tz, now)

    await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=member.id,
        text=entry.title,
        fire_at=fire_at,
        entry_id=entry.id,
    )
    return ""


@router.message(_awaits)
async def take_day(
    message: Message, session: AsyncSession, family: Family, bot: Bot
) -> None:
    """«Другая дата» ответом на сообщение разбора."""
    key = (message.chat.id, message.reply_to_message.message_id)
    entry_id = _pending.pop(key)

    entry = await _live_entry(session, entry_id, family)
    if entry is None:
        await message.reply(texts.REVIEW_STALE)
        return

    now = tu.now_utc()
    parsed = nlp.parse_when((message.text or "").strip(), tu.to_local(now, family.tz))
    if parsed is None:
        _pending[key] = entry_id  # ожидание не снимаем: перенос не состоялся
        await message.reply(texts.REVIEW_BAD_DATE)
        return

    # Берём только день: время суток у записи своё, и придуманное `dateparser`
    # «сейчас» (`capture` обходит это отдельно) сюда просто не попадает
    moved = await entries.move(session, entry, family, parsed.when.date())
    if moved is None:
        await message.reply(texts.REVIEW_STALE)
        return

    panel.schedule(bot, family.id, message.message_id)
    await sending.edit(
        bot,
        family,
        key[1],
        texts.REVIEW_ASK_REMIND.format(when=_moved_to(moved, family, now)),
        kb.review_remind_keyboard(moved.id, all_day=moved.all_day),
    )
