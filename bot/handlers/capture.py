"""Свободный текст → карточка подтверждения → запись (шаги 3a.6, 3b.1–3b.5).

Основной путь создания записей: `IsTrigger` решает, что сообщение адресовано
боту, `parsing` строит промпт и приводит ответ модели в порядок, а здесь всё
это превращается в карточку и, после нажатия, в строки `entries` и `reminders`.

Ничего не сохраняется молча — инвариант проекта: между разбором и базой
всегда стоит человек с кнопкой.

Когда модель недоступна, разбор не умирает: в дело идёт `nlp_fallback` на
`dateparser` (шаг 3b.1). Он беднее — знает только дату и остаток текста, — и
карточка честно об этом говорит.

Роутер стоит **последним**, после мастера `/new`. Соблазн поставить его раньше
(как `views` и `remind`) разбивается о то, что обращением считается и ответ на
сообщение бота: мастер спрашивает «Что записать?», человек отвечает реплаем — и
этот хендлер украл бы шаг мастера. Позади мастера такого не случается, там
раньше срабатывает `StateFilter(New)`. Цена — `+…`, набранное посреди `/new`,
получит «сейчас идёт запись через /new» вместо разбора; это честнее молчаливого
обрыва мастера. По той же причине роутеру не нужен `drop_wizard_state`: если
сообщение дошло сюда, значит мастер на него не претендовал.

**Внутри роутера порядок тоже значим:** хендлер правки (`edit_field`) стоит
выше хендлера с `IsTrigger`. Правка приходит реплаем на карточку, а реплай на
сообщение бота — это обращение по всем признакам фильтра; стой он ниже, новый
текст записи уехал бы в модель как отдельная фраза.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, time

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Entry, Family, Member
from bot.filters import IN_GROUP, IN_GROUP_CB, IsTrigger
from bot.handlers import lists
from bot.handlers.views import edit_or_ignore
from bot.services import llm, panel, parse_log, parsing, sending, ticker
from bot.services import nlp_fallback as nlp
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

log = logging.getLogger(__name__)

# Столько карточек одновременно в семейном чате не висит. Потолок нужен не ради
# памяти на одну карточку, а чтобы брошенные черновики не копились всё время
# работы бота
MAX_DRAFTS = 50

# Ответы, которыми у записи снимают дату. Отдельной кнопки нет: правка даты и
# так идёт текстом, а «без даты» — самое естественное, что человек напишет
NO_DATE_WORDS = frozenset({"без даты", "убрать", "убрать дату", "никогда", "-"})


@dataclass(slots=True)
class Draft:
    """Разбор, ждущий нажатия. Времена внутри `items` — локальные, не UTC."""

    family_id: int
    items: list[parsing.Item]
    source_message_id: int  # исходная фраза, а не карточка
    via: str = "llm"  # 'llm' | 'dateparser' — каким путём разобрано
    edited: bool = False  # правил ли человек разбор кнопками


# Ключ — (chat_id, message_id) карточки. Не FSM: его ключ «чат + пользователь»,
# и «Сохранить», нажатое не автором фразы, черновика бы не нашло — а в семейном
# чате это обычное дело. Живёт в памяти и умирает с перезапуском, ровно как
# состояние мастера /new
_drafts: dict[tuple[int, int], Draft] = {}

# Карточки, которые ждут ответа реплаем: ключ тот же, значение — что правим
# ('date' | 'text'). Отдельный словарь, а не поле `Draft`, потому что живёт он
# короче черновика: ожидание снимается первым же ответом
_pending: dict[tuple[int, int], str] = {}


def _remember(key: tuple[int, int], draft: Draft) -> None:
    if len(_drafts) >= MAX_DRAFTS:
        # Словари Python держат порядок вставки — первый ключ самый старый
        _forget(next(iter(_drafts)))
    _drafts[key] = draft


def _forget(key: tuple[int, int]) -> None:
    """Снять черновик вместе с ожиданием правки — они живут одной жизнью."""
    _drafts.pop(key, None)
    _pending.pop(key, None)


def _card(draft: Draft, tz: str) -> tuple[str, object]:
    """Текст карточки и клавиатура к нему — всегда вместе.

    Подпись кнопки зависит от того же, что и `⚠️` в тексте (шаг 3b.2), поэтому
    собираются они в одном месте: разъехавшись, они однажды покажут
    предупреждение без кнопки или наоборот.
    """
    text = texts.capture_card(draft.items, tz, via=draft.via)
    warn = any(texts.is_past(item, tz) for item in draft.items)
    markup = kb.capture_keyboard(warn=warn, editable=len(draft.items) == 1)
    return text, markup


def _awaits_edit(message: Message) -> bool:
    """Это ответ на карточку, которая ждёт правки?

    Фильтр обязан быть точным: он стоит выше `IsTrigger`, и всё, на что он
    ответит «да», до разбора уже не дойдёт.
    """
    reply = message.reply_to_message
    return reply is not None and (message.chat.id, reply.message_id) in _pending


@router.message(_awaits_edit)
async def edit_field(message: Message, family: Family, bot: Bot) -> None:
    """Новый текст или новая дата ответом на карточку (шаги 3b.3 и 3b.5)."""
    key = (message.chat.id, message.reply_to_message.message_id)
    field = _pending.pop(key)
    draft = _drafts.get(key)
    if draft is None or draft.family_id != family.id:
        # Между тапом и ответом карточку успели сохранить, отменить или вытеснить
        await message.reply(texts.CAPTURE_STALE)
        return

    item = draft.items[0]
    raw = (message.text or "").strip()

    if field == "text":
        title = " ".join(raw.split())[: parsing.TITLE_LIMIT]
        if not title:
            _pending[key] = field  # ожидание не снимаем: правка не состоялась
            await message.reply(texts.CAPTURE_BAD_TEXT)
            return
        draft.items[0] = replace(item, title=title)
    elif raw.lower() in NO_DATE_WORDS:
        draft.items[0] = replace(item, due_at=None, all_day=False)
    else:
        parsed = nlp.parse_when(raw, tu.to_local(tu.now_utc(), family.tz))
        if parsed is None:
            _pending[key] = field
            await message.reply(texts.CAPTURE_BAD_DATE)
            return
        draft.items[0] = replace(
            item,
            due_at=parsed.when,
            # Названа дата без времени — это «весь день». Тот же вывод, что
            # делает `parsing._item` для ответа модели
            all_day=parsed.when.time() == time(0, 0),
        )

    draft.edited = True
    text, markup = _card(draft, family.tz)
    # Не `edit_or_ignore`: правка пришла сообщением, а не колбэком, и объекта
    # `CallbackQuery` тут нет. Политика ошибок Telegram — в `sending`
    if await sending.edit(bot, family, key[1], text, markup) != sending.OK:
        await message.reply(texts.CAPTURE_EDIT_FAILED)


@router.message(IsTrigger())
async def capture(
    message: Message,
    payload: str,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """`payload` — текст без признака обращения, его готовит `IsTrigger`."""
    await handle_phrase(message, payload, session, family, member, bot)


async def handle_phrase(
    message: Message,
    text: str,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """Фраза → разбор → карточка. Зовут двое: текстовый `capture` и голос.

    Вынесено из `capture` ради этапа 5: расшифрованное голосовое обязано идти
    ровно тем же путём, что и текст, включая запасной
    разбор, автосохранение и лог. `message` при этом остаётся исходным
    сообщением человека — от него берётся `source_message_id` карточки.
    """
    members = await repo.members_of(session, family.id)
    shopping = await repo.active_list(session, family.id)
    system = parsing.build_system(
        tu.to_local(tu.now_utc(), family.tz),
        family.tz,
        [m.display_name for m in members],
        [shopping.name] if shopping else [],
    )

    raw = await llm.ask(system, text)
    if raw is None:
        # Сеть, ключ, отказ провайдера — наружу всё это приходит одинаково
        await _fallback(message, text, family)
        return

    intent, items = parsing.normalize(raw)
    log.info("Разбор: intent=%s, записей=%s", intent, len(items))
    parse_log.write(
        event="parse",
        via="llm",
        model=settings.openrouter_model,
        chat=message.chat.id,
        text=text,
        intent=intent,
        items=len(items),
        answer=raw,
    )

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

    draft = Draft(family.id, items, message.message_id, "llm")
    if _autosave_ready(items, family.tz):
        await _autosave(message, draft, session, family, member, bot)
        return
    await _show_card(message, draft, family.tz)


def _has_shopping(draft: Draft) -> bool:
    """Есть ли в разборе покупка — значит, панель списка устарела."""
    return any(item.kind == "shopping" for item in draft.items)


def _autosave_ready(items: list[parsing.Item], tz: str) -> bool:
    """Разбор настолько уверенный, что карточку можно не показывать (3b.6).

    Выключено по умолчанию (`AUTOSAVE_CONFIDENCE=0`): инвариант «ничего не
    сохраняется молча» снимается осознанно и только когда по `parse.log` видно,
    что модель на высокой уверенности не ошибается. Запись с датой в прошлом не
    автосохраняется никогда — именно там ошибка разбора и стоит дороже всего.
    """
    threshold = settings.autosave_confidence
    if threshold <= 0:
        return False
    return all(
        item.confidence >= threshold and not texts.is_past(item, tz) for item in items
    )


async def _autosave(
    message: Message,
    draft: Draft,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    text = await _save(message.chat.id, draft, session, family, member, texts.SAVED_AUTO)
    parse_log.write(
        event="verdict", verdict="autosaved", via=draft.via, chat=message.chat.id
    )
    panel.schedule(bot, family.id, message.message_id)
    if _has_shopping(draft):
        await lists.refresh_panel(bot, session, family, message)
    await message.answer(text)


async def _fallback(message: Message, payload: str, family: Family) -> None:
    """Разбор без модели — `dateparser` и ничего больше (шаг 3b.1).

    Умеет он мало: тип записи не определяет, повторяемость не понимает вовсе.
    Зато бот остаётся живым при отвалившемся OpenRouter — это и есть требование
    «не падать и не молчать из-за внешнего сервиса».
    """
    if nlp.looks_recurring(payload):
        # `dateparser` выбросит слово «каждый» и молча сделает повтор разовым.
        # Тот же честный отказ, что и в `/remind`
        parse_log.write(
            event="parse",
            via="dateparser",
            chat=message.chat.id,
            text=payload,
            result="recurring",
        )
        await message.answer(texts.CAPTURE_RECURRING_FALLBACK)
        return

    now_local = tu.to_local(tu.now_utc(), family.tz)
    parsed = nlp.parse_when(payload, now_local)
    if parsed is None or not parsed.text:
        parse_log.write(
            event="parse",
            via="dateparser",
            chat=message.chat.id,
            text=payload,
            result="failed",
        )
        await message.answer(texts.CAPTURE_FAILED)
        return

    # Время, которого никто не называл, `dateparser` берёт из «сейчас»: сказано
    # «завтра» в 14:37 — получите «завтра в 14:37». Показать придуманное время
    # хуже, чем показать один день: человек подтверждает карточку кнопкой и
    # унесёт выдумку в базу. Признак «время не названо» — совпадение часа и
    # минуты с текущими; цена — «через сутки» тоже станет записью на весь день,
    # но это куда более редкая фраза, чем «завтра»
    invented = (parsed.when.hour, parsed.when.minute) == (
        now_local.hour,
        now_local.minute,
    )
    all_day = invented or parsed.when.time() == time(0, 0)
    due_at = (
        parsed.when.replace(hour=0, minute=0, second=0, microsecond=0)
        if all_day
        else parsed.when
    )

    parse_log.write(
        event="parse",
        via="dateparser",
        chat=message.chat.id,
        text=payload,
        result="ok",
        title=parsed.text,
        due_at=due_at.isoformat(),
        all_day=all_day,
    )
    item = parsing.Item(
        kind=parsing.DEFAULT_KIND,  # тип определять нечем — пусть человек поправит
        title=parsed.text[: parsing.TITLE_LIMIT],
        due_at=due_at,
        all_day=all_day,
        # Ровно на пороге: `uncertain` не сработает, и карточка не скажет дважды
        # одно и то же — про слабость этого разбора говорит `CAPTURE_VIA_FALLBACK`
        confidence=parsing.LOW_CONFIDENCE,
    )
    await _show_card(
        message, Draft(family.id, [item], message.message_id, "dateparser"), family.tz
    )


async def _show_card(message: Message, draft: Draft, tz: str) -> None:
    preview, markup = _card(draft, tz)
    if len(preview) > texts.MESSAGE_LIMIT:
        # Обрезать карточку нельзя: человек подтвердил бы кнопкой то, чего не
        # видел. А отправить как есть — значит получить отказ Telegram и
        # промолчать на пустом месте
        log.warning("Разбор длиной %s символов не показать", len(preview))
        await message.answer(texts.CAPTURE_TOO_LONG)
        return

    card = await message.answer(preview, reply_markup=markup)
    _remember((message.chat.id, card.message_id), draft)


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

    action = callback_data.action
    if action in ("save", "cancel"):
        await _finish(call, action, key, draft, session, family, member, bot)
        return

    # Дальше идёт правка: черновик остаётся на месте, меняется только карточка.
    # Именно поэтому черновик снимается не здесь, а в `_finish`: сними его на
    # любом тапе, как было в 3a, — и первое же нажатие «Дата» его бы убило
    if action == "kind":
        text, _ = _card(draft, family.tz)
        await call.answer()
        await edit_or_ignore(call, text, kb.capture_kind_keyboard())
        return

    if action == "setkind":
        if callback_data.kind in parsing.KINDS:
            draft.items[0] = replace(draft.items[0], kind=callback_data.kind)
            draft.edited = True
        action = "back"  # дальше — обычная перерисовка карточки

    if action == "back":
        _pending.pop(key, None)
        text, markup = _card(draft, family.tz)
        await call.answer()
        await edit_or_ignore(call, text, markup)
        return

    if action in ("date", "text"):
        await _ask_edit(call, key, draft, family, action)


async def _ask_edit(
    call: CallbackQuery, key: tuple[int, int], draft: Draft, family: Family, field: str
) -> None:
    """Попросить ответить на карточку новым значением (шаги 3b.3 и 3b.5)."""
    prompt = texts.CAPTURE_ASK_DATE if field == "date" else texts.CAPTURE_ASK_TEXT
    text, markup = _card(draft, family.tz)
    full = f"{text}\n\n{prompt}"
    if len(full) > texts.MESSAGE_LIMIT:
        # Длинная карточка с подсказкой не влезла: подсказка уходит всплывающим
        # окном, карточка остаётся как была
        full = text
        await call.answer(prompt, show_alert=True)
    else:
        await call.answer()
    _pending[key] = field
    await edit_or_ignore(call, full, markup)


async def _finish(
    call: CallbackQuery,
    action: str,
    key: tuple[int, int],
    draft: Draft,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    # Черновик снимается до сохранения, а не после: двое могут нажать
    # «Сохранить» на одной карточке, и второй тап обязан не создать вторую пару
    # записей, а получить «карточка устарела»
    _forget(key)
    parse_log.write(
        event="verdict",
        verdict="saved" if action == "save" else "cancelled",
        edited=draft.edited,
        via=draft.via,
        chat=key[0],
        card=key[1],
    )

    if action == "cancel":
        await call.answer()
        await edit_or_ignore(call, texts.CAPTURE_CANCELLED, None)
        return

    text = await _save(call.message.chat.id, draft, session, family, member)
    await call.answer()
    # Раньше перерисовки карточки: записи уже в базе, и сбой рендера не должен
    # оставить закреплённую панель без них
    panel.schedule(bot, family.id, call.message.message_id)
    if _has_shopping(draft):
        await lists.refresh_panel(bot, session, family, call.message)
    await edit_or_ignore(call, text, None)


async def _save(
    chat_id: int,
    draft: Draft,
    session: AsyncSession,
    family: Family,
    member: Member,
    head: str = texts.SAVED,
) -> str:
    """Черновик → записи и напоминания. Возвращает готовый текст ответа."""
    now = tu.now_utc()
    cards: list[str] = []
    notes: list[str] = []

    for item in draft.items:
        due_at = tu.to_utc(item.due_at, family.tz) if item.due_at else None
        # Покупка обязана попасть в список — иначе она не видна нигде, кроме
        # `/find`: `/buy` смотрит на `list_id`, день и «Просрочено» на `due_at`
        # (у покупки его чаще нет), `/tasks` и `/notes` на `kind`
        list_id = position = None
        if item.kind == "shopping":
            list_id, position = await repo.shopping_slot(session, family.id)
        entry = await repo.create_entry(
            session,
            family_id=family.id,
            author_id=member.id,
            kind=item.kind,
            title=item.title,
            body=item.body,
            due_at=due_at,
            all_day=item.all_day,
            list_id=list_id,
            position=position,
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
    return texts.join_under_limit([head, *cards, *notes])


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
