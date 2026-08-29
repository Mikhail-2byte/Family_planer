"""Карточка сохранённой записи: переписать, передвинуть срок, удалить (этап 7).

До этого этапа записанное было неприкосновенно. Кнопки правки жили только в
карточке подтверждения, то есть **до** сохранения, а удаления не было нигде.

Устроено по образцу разбора незакрытого (`handlers/review.py`), и по той же
причине: **состояния почти нет**. И запись, и страница, куда возвращает
«← Назад», едут прямо в `callback_data`, поэтому кнопки переживают перезапуск
бота. В памяти живёт только ожидание ответа реплаем — иначе его не с чем
связать.

**Роутер обязан стоять раньше мастера `/new`.** Новый текст и «другую дату»
отвечают реплаем на сообщение бота, а реплай `IsTrigger` считает обращением:
стой роутер позади `capture`, «в пятницу» уехало бы в модель отдельным платным
запросом. Ровно та же причина, по которой раньше мастера стоят `review`,
`settings` и `voice`.

Вход в карточку — кнопка «✏️ N» на странице `/tasks`, `/notes` или `/events`.
В `/today`, `/week` и панели её нет и быть не может: их собирает общий
`digest.build_day`, и нумерация протекла бы разом в утреннюю сводку и в
закреплённое сообщение.

Тип записи здесь не меняется, в отличие от карточки разбора: сменить его у
сохранённой записи значит выдать или отобрать слот в списке покупок.
"""

import logging
from datetime import timedelta

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Entry, Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.handlers.views import _render_page, edit_or_ignore
from bot.services import entries as entry_service
from bot.services import nlp_fallback as nlp
from bot.services import panel, parsing, sending
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

log = logging.getLogger(__name__)

# Ключ — (chat_id, message_id) сообщения, на котором висит карточка; значение —
# какую запись правим, чего ждём ('text' | 'date') и на какую страницу
# возвращать. Страница едет и здесь, а не только в `callback_data`: правка
# приходит **сообщением**, и колбэка с ней нет.
#
# Потолок обязателен, и здесь даже нужнее, чем в разборе: карточка живёт на
# сообщении **страницы** `/tasks`, а не на отдельном сообщении разбора, и люди
# листают чат назад и отвечают на старые списки.
_pending: dict[tuple[int, int], tuple[int, str, str, int]] = {}
MAX_PENDING = 20

# Те же слова, что снимают дату в карточке разбора
NO_DATE_WORDS = frozenset({"без даты", "убрать", "убрать дату", "никогда", "-"})


def _awaits(message: Message) -> bool:
    """Это ответ на карточку, которая ждёт текста или даты?

    Фильтр обязан быть точным: роутер стоит раньше мастера и раньше `capture`,
    и всё, на что он ответит «да», до разбора уже не дойдёт.
    """
    reply = message.reply_to_message
    return reply is not None and (message.chat.id, reply.message_id) in _pending


def _remember(
    key: tuple[int, int], entry_id: int, field: str, view: str, offset: int
) -> None:
    if len(_pending) >= MAX_PENDING and key not in _pending:
        _pending.pop(next(iter(_pending)))  # dict хранит порядок вставки
    _pending[key] = (entry_id, field, view, offset)


async def _live_entry(
    session: AsyncSession, entry_id: int, family: Family
) -> Entry | None:
    """Запись, которую ещё можно править. Изоляция по семье — обязательна.

    Проверка есть и здесь, и в `repo`, и это не дублирование: `repo` стережёт
    правку, а хендлер — чтение, потому что карточка показывает заголовок.
    Кнопки висят в чате вечно, и за это время запись могли закрыть или удалить.
    """
    entry = await repo.get_entry(session, entry_id)
    if entry is None or entry.family_id != family.id or entry.status != "open":
        return None
    return entry


async def _card_text(session: AsyncSession, entry: Entry, family: Family) -> str:
    # Без `refresh` карточка подпишет «кто-то»: автор — ленивая связь, а
    # `repo.get_entry` её не тянет
    await session.refresh(entry, ["author"])
    return f"{texts.entry_card(entry, family.tz)}\n\n{texts.ENTRY_HINT}"


async def _show_card(
    call: CallbackQuery,
    session: AsyncSession,
    entry: Entry,
    family: Family,
    view: str,
    offset: int,
) -> None:
    text = await _card_text(session, entry, family)
    await edit_or_ignore(call, text, kb.entry_card_keyboard(entry.id, view, offset))


async def _back_to_list(
    call: CallbackQuery,
    session: AsyncSession,
    family: Family,
    view: str,
    offset: int,
    *,
    undo: int | None = None,
) -> None:
    """Вернуть на страницу списка, при необходимости — с кнопкой отката.

    Ряд «↩️ Вернуть» дописывается к готовой клавиатуре и живёт до следующей
    перерисовки. Этого достаточно: откат нужен сразу после промаха, а не через
    неделю. Пустая страница приходит вообще без разметки — тогда ряд становится
    единственным, иначе кнопку некуда было бы повесить.
    """
    text, markup = await _render_page(session, family, view, offset)
    if undo is not None:
        row = kb.entry_undo_row(undo, view, offset)
        rows = [*markup.inline_keyboard, row] if markup is not None else [row]
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await edit_or_ignore(call, text, markup)


def _when(entry: Entry, family: Family) -> str:
    return tu.fmt_due(entry.due_at, family.tz, all_day=entry.all_day)


@router.callback_query(kb.EntryCB.filter())
async def tap(
    call: CallbackQuery,
    callback_data: kb.EntryCB,
    session: AsyncSession,
    family: Family,
    bot: Bot,
) -> None:
    """Все кнопки карточки одним хендлером — как `capture.tap` и `review.tap`."""
    action = callback_data.action
    entry_id = callback_data.entry_id
    view = callback_data.view
    offset = callback_data.offset
    key = (call.message.chat.id, call.message.message_id)

    # Откат удаления идёт первым: `_live_entry` удалённую запись не отдаст
    if action == "undo":
        restored = await repo.unarchive_entry(session, entry_id, family.id)
        if restored is None:
            await call.answer(texts.ENTRY_GONE, show_alert=True)
        else:
            await call.answer(texts.entry_restored(restored.title))
            panel.schedule(bot, family.id, call.message.message_id)
        await _back_to_list(call, session, family, view, offset)
        return

    entry = await _live_entry(session, entry_id, family)
    if entry is None:
        # Записи нет, она чужая или её успели закрыть — во всех трёх случаях
        # говорим одно и то же и возвращаем к списку, который уже не тот
        _pending.pop(key, None)
        await call.answer(texts.ENTRY_GONE, show_alert=True)
        await _back_to_list(call, session, family, view, offset)
        return

    if action in ("open", "back"):
        # «← Назад» с экранов даты и удаления приходит как 'open': ожидание
        # правки снимаем, иначе ответ реплаем после возврата всё ещё считался бы
        # правкой — грабля, выстраданная в `capture.tap`
        _pending.pop(key, None)
        await call.answer()
        if action == "back":
            await _back_to_list(call, session, family, view, offset)
        else:
            await _show_card(call, session, entry, family, view, offset)
        return

    if action == "text":
        _remember(key, entry.id, "text", view, offset)
        await call.answer()
        card = await _card_text(session, entry, family)
        await edit_or_ignore(
            call,
            f"{card}\n\n{texts.ENTRY_ASK_TEXT}",
            kb.entry_card_keyboard(entry.id, view, offset),
        )
        return

    if action in ("date", "other"):
        # Экран дня один на обе кнопки: у 'other' к нему добавляется приглашение
        # ответить реплаем, а кнопки дней остаются доступны — как в разборе
        card = await _card_text(session, entry, family)
        tail = texts.ENTRY_ASK_DATE if action == "other" else ""
        if action == "other":
            _remember(key, entry.id, "date", view, offset)
        else:
            _pending.pop(key, None)
        await call.answer()
        await edit_or_ignore(
            call,
            f"{card}\n\n{tail}" if tail else card,
            kb.entry_date_keyboard(
                entry.id, view, offset, has_date=entry.due_at is not None
            ),
        )
        return

    if action in ("day", "nodate"):
        if action == "day":
            # `value` заполнено только здесь — это сдвиг в днях от сегодня
            target = tu.local_today(family.tz) + timedelta(days=callback_data.value)
            updated = await entry_service.move(session, entry, family, target)
            note = (
                texts.ENTRY_DATE_SAVED.format(when=_when(updated, family))
                if updated is not None
                else ""
            )
        else:
            updated = await entry_service.clear_due(session, entry, family)
            note = texts.ENTRY_DATE_CLEARED
        if updated is None:
            await call.answer(texts.ENTRY_GONE, show_alert=True)
            await _back_to_list(call, session, family, view, offset)
            return
        _pending.pop(key, None)
        await call.answer(note)
        # Раньше перерисовки: срок уже в базе, и сбой рендера карточки не должен
        # оставить панель дня с прежней датой
        panel.schedule(bot, family.id, call.message.message_id)
        await _show_card(call, session, updated, family, view, offset)
        return

    if action == "who":
        # Ожидание правки снимаем: экран сменился, и ответ реплаем после него
        # правкой уже не считается — та же грабля, что у 'date' и 'del'
        _pending.pop(key, None)
        members = await repo.members_of(session, family.id)
        card = await _card_text(session, entry, family)
        await call.answer()
        await edit_or_ignore(
            call,
            f"{card}\n\n{texts.entry_ask_who(entry.title)}",
            kb.entry_assignee_keyboard(
                entry.id,
                view,
                offset,
                members,
                assigned=entry.assignee_id is not None,
            ),
        )
        return

    if action == "setwho":
        # 0 — «ничьё»: поле у `CallbackData` целочисленное, а участника с таким
        # id не бывает. Семью участника проверяет сам `repo.set_assignee`
        target_id = callback_data.value or None
        updated = await repo.set_assignee(session, entry.id, family.id, target_id)
        if updated is None:
            await call.answer(texts.ENTRY_GONE, show_alert=True)
            await _back_to_list(call, session, family, view, offset)
            return
        note = (
            texts.entry_who_saved(updated.assignee.display_name)
            if updated.assignee is not None
            else texts.ENTRY_WHO_CLEARED
        )
        await call.answer(note)
        # Панель дня показывает те же записи, а поручение видно в строке списка
        panel.schedule(bot, family.id, call.message.message_id)
        await _show_card(call, session, updated, family, view, offset)
        return

    if action == "del":
        _pending.pop(key, None)
        await call.answer()
        await edit_or_ignore(
            call,
            texts.entry_ask_delete(entry.title),
            kb.entry_delete_keyboard(entry.id, view, offset),
        )
        return

    # Осталось 'yes' — подтверждённое удаление. Проверка живости нужна и здесь:
    # между «🗑» и «Да, удалить» второй человек мог запись закрыть
    archived = await repo.archive_entry(session, entry.id, family.id)
    if archived is None:
        await call.answer(texts.ENTRY_GONE, show_alert=True)
        await _back_to_list(call, session, family, view, offset)
        return

    await call.answer(texts.entry_deleted(archived.title))
    panel.schedule(bot, family.id, call.message.message_id)
    await _back_to_list(call, session, family, view, offset, undo=archived.id)


@router.message(_awaits)
async def take_reply(
    message: Message, session: AsyncSession, family: Family, bot: Bot
) -> None:
    """Новый текст или новая дата ответом на карточку.

    Скелет тот же, что у `capture.edit_field` и `review.take_day`: ожидание
    снимается сразу, а при неудачном разборе **возвращается** — правка не
    состоялась, и человек обязан иметь возможность ответить ещё раз.

    Из даты берётся только день: время суток у записи своё, и придуманное
    `dateparser` «сейчас» сюда не попадает. Про это прямо сказано в приглашении.
    """
    key = (message.chat.id, message.reply_to_message.message_id)
    entry_id, field, view, offset = _pending.pop(key)

    entry = await _live_entry(session, entry_id, family)
    if entry is None:
        await message.reply(texts.ENTRY_GONE)
        return

    raw = (message.text or "").strip()

    if field == "text":
        title = " ".join(raw.split())[: parsing.TITLE_LIMIT]
        if not title:
            _pending[key] = (entry_id, field, view, offset)  # правка не состоялась
            await message.reply(texts.ENTRY_BAD_TEXT)
            return
        updated = await repo.edit_entry_title(session, entry.id, family.id, title)
    elif raw.lower() in NO_DATE_WORDS:
        updated = await entry_service.clear_due(session, entry, family)
    else:
        parsed = nlp.parse_when(raw, tu.to_local(tu.now_utc(), family.tz))
        if parsed is None:
            _pending[key] = (entry_id, field, view, offset)
            await message.reply(texts.ENTRY_BAD_DATE)
            return
        updated = await entry_service.move(
            session, entry, family, parsed.when.date()
        )

    if updated is None:
        await message.reply(texts.ENTRY_GONE)
        return

    panel.schedule(bot, family.id, message.message_id)
    text = await _card_text(session, updated, family)
    markup = kb.entry_card_keyboard(updated.id, view, offset)
    # Не `edit_or_ignore`: правка пришла сообщением, а не колбэком, и объекта
    # `CallbackQuery` тут нет. Политика ошибок Telegram — в `sending`
    if await sending.edit(bot, family, key[1], text, markup) != sending.OK:
        await message.reply(texts.ENTRY_EDIT_FAILED)
