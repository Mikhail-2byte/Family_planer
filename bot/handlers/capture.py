"""Свободный текст → карточка подтверждения → запись (шаг 3a.6).

Основной путь создания записей: `IsTrigger` решает, что сообщение адресовано
боту, `parsing` строит промпт и приводит ответ модели в порядок, а здесь всё
это превращается в карточку и, после нажатия, в строки `entries` и `reminders`.

Ничего не сохраняется молча — инвариант `PLAN.md`: между разбором и базой
всегда стоит человек с кнопкой.

Роутер стоит **последним**, после мастера `/new`. Соблазн поставить его раньше
(как `views` и `remind`) разбивается о то, что обращением считается и ответ на
сообщение бота: мастер спрашивает «Что записать?», человек отвечает реплаем — и
этот хендлер украл бы шаг мастера. Позади мастера такого не случается, там
раньше срабатывает `StateFilter(New)`. Цена — `+…`, набранное посреди `/new`,
получит «сейчас идёт запись через /new» вместо разбора; это честнее молчаливого
обрыва мастера. По той же причине роутеру не нужен `drop_wizard_state`: если
сообщение дошло сюда, значит мастер на него не претендовал.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB, IsTrigger
from bot.handlers.views import edit_or_ignore
from bot.services import llm, panel, parsing, ticker
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

log = logging.getLogger(__name__)

# Столько карточек одновременно в семейном чате не висит. Потолок нужен не ради
# памяти на одну карточку, а чтобы брошенные черновики не копились всё время
# работы бота
MAX_DRAFTS = 50


@dataclass(slots=True)
class Draft:
    """Разбор, ждущий нажатия. Времена внутри `items` — локальные, не UTC."""

    family_id: int
    items: list[parsing.Item]
    source_message_id: int  # исходная фраза, а не карточка


# Ключ — (chat_id, message_id) карточки. Не FSM: его ключ «чат + пользователь»,
# и «Сохранить», нажатое не автором фразы, черновика бы не нашло — а в семейном
# чате это обычное дело. Живёт в памяти и умирает с перезапуском, ровно как
# состояние мастера /new
_drafts: dict[tuple[int, int], Draft] = {}


def _remember(key: tuple[int, int], draft: Draft) -> None:
    if len(_drafts) >= MAX_DRAFTS:
        # Словари Python держат порядок вставки — первый ключ самый старый
        _drafts.pop(next(iter(_drafts)))
    _drafts[key] = draft


@router.message(IsTrigger())
async def capture(
    message: Message, payload: str, session: AsyncSession, family: Family
) -> None:
    """`payload` — текст без признака обращения, его готовит `IsTrigger`."""
    members = await repo.members_of(session, family.id)
    system = parsing.build_system(
        tu.to_local(tu.now_utc(), family.tz),
        family.tz,
        [m.display_name for m in members],
        [],  # списков до этапа 4 не существует
    )

    raw = await llm.ask(system, payload)
    if raw is None:
        # Сеть, ключ, отказ провайдера — наружу всё это приходит одинаково.
        # Запасной разбор через dateparser подключается на шаге 3b.1
        await message.answer(texts.CAPTURE_FAILED)
        return

    intent, items = parsing.normalize(raw)
    log.info("Разбор: intent=%s, записей=%s", intent, len(items))

    if intent == "chitchat":
        return  # молчание — критерий закрытия этапа 3a
    if intent != "create":
        # query и complete модель различает, но обработки у них ещё нет.
        # Молчать нельзя: к боту обратились явно, и тишина читается как поломка
        await message.answer(texts.CAPTURE_NOT_YET)
        return
    if not items:
        await message.answer(texts.CAPTURE_EMPTY)
        return

    preview = texts.capture_card(items, family.tz)
    if len(preview) > texts.MESSAGE_LIMIT:
        # Обрезать карточку нельзя: человек подтвердил бы кнопкой то, чего не
        # видел. А отправить как есть — значит получить отказ Telegram и
        # промолчать на пустом месте
        log.warning("Разбор длиной %s символов не показать", len(preview))
        await message.answer(texts.CAPTURE_TOO_LONG)
        return

    card = await message.answer(preview, reply_markup=kb.capture_keyboard())
    _remember(
        (message.chat.id, card.message_id),
        Draft(family.id, items, message.message_id),
    )


@router.callback_query(kb.CaptureCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: kb.CaptureCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    key = (call.message.chat.id, call.message.message_id)
    draft = _drafts.get(key)
    if draft is None:
        # Перезапуск бота или карточка, пережившая вытеснение по MAX_DRAFTS
        await call.answer(texts.CAPTURE_STALE, show_alert=True)
        return
    if draft.family_id != family.id:
        # Изоляция по семье — инвариант проекта. Ключ уже включает chat_id, так
        # что обычным путём сюда не попасть: это страховка на случай, если
        # соответствие «чат ↔ семья» когда-нибудь перестанет быть однозначным
        await call.answer(texts.CAPTURE_ALIEN, show_alert=True)
        return

    # Черновик снимается до сохранения, а не после: двое могут нажать
    # «Сохранить» на одной карточке, и второй тап обязан не создать вторую пару
    # записей, а получить «карточка устарела»
    del _drafts[key]
    if callback_data.action == "cancel":
        await call.answer()
        await edit_or_ignore(call, texts.CAPTURE_CANCELLED, None)
        return

    text = await _save(call.message.chat.id, draft, session, family, member)
    await call.answer()
    # Раньше перерисовки карточки: записи уже в базе, и сбой рендера не должен
    # оставить закреплённую панель без них
    panel.schedule(bot, family.id, call.message.message_id)
    await edit_or_ignore(call, text, None)


async def _save(
    chat_id: int,
    draft: Draft,
    session: AsyncSession,
    family: Family,
    member: Member,
) -> str:
    """Черновик → записи и напоминания. Возвращает готовый текст ответа."""
    now = tu.now_utc()
    cards: list[str] = []
    notes: list[str] = []

    for item in draft.items:
        due_at = tu.to_utc(item.due_at, family.tz) if item.due_at else None
        entry = await repo.create_entry(
            session,
            family_id=family.id,
            author_id=member.id,
            kind=item.kind,
            title=item.title,
            body=item.body,
            due_at=due_at,
            all_day=item.all_day,
            source_chat_id=chat_id,
            # Мастер /new пишет только chat_id — здесь есть настоящий оригинал,
            # и по нему карточка даёт ссылку на исходное сообщение (шаг 3a.7)
            source_message_id=draft.source_message_id,
        )
        notes += await _remind(session, family, member, entry, item, due_at, now)
        # Автор подгружается отдельно: без этого `entry_card` подпишет «кто-то»
        await session.refresh(entry, ["author"])
        cards.append(texts.entry_card(entry, family.tz, now))

    # Здесь, в отличие от карточки, обрезка безопасна: записи уже в базе, и
    # подрезается только эхо. А вот отказ Telegram по длине оставил бы человека
    # с крутящимся «часиком» над уже сохранёнными записями — и он нажал бы ещё раз
    return texts.join_under_limit([texts.SAVED, *cards, *notes])


async def _remind(
    session: AsyncSession,
    family: Family,
    member: Member,
    entry: Entry,
    item: parsing.Item,
    due_at: datetime | None,
    now: datetime,
) -> list[str]:
    """Напоминания одной записи. Возвращает оговорки к ответу, если они есть."""
    notes: list[str] = []

    for moment in item.reminders:
        fire_at = tu.to_utc(moment, family.tz)
        if fire_at <= now:
            # Тикер честно отработал бы это догонкой и выстрелил в ближайший
            # тик — сюрприз на пустом месте. `/new` и `/remind` поступают так же
            notes.append(texts.remind_in_past(fire_at, family.tz, now))
            continue
        await repo.create_reminder(
            session,
            family_id=family.id,
            created_by=member.id,
            text=entry.title,
            fire_at=fire_at,
            entry_id=entry.id,
        )

    if item.rrule:
        # Тикер повторяемость умеет с этапа 2, но завести её до сих пор было
        # нечем: ни `/new`, ни `/remind` `rrule` не создают.
        # Якорь обрезан до минут: у правила без BYSECOND секунды берутся из
        # него, и серия навсегда осталась бы со «19:00:37» от момента создания
        anchor = (due_at or now).replace(second=0, microsecond=0)
        first = ticker.next_fire_at(item.rrule, anchor, now, family.tz)
        if first is None:
            notes.append(texts.capture_rrule_bad(item.rrule))
        else:
            await repo.create_reminder(
                session,
                family_id=family.id,
                created_by=member.id,
                text=entry.title,
                fire_at=first,
                entry_id=entry.id,
                rrule=item.rrule,
            )

    return notes
