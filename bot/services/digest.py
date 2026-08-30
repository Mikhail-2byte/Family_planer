"""Утренняя сводка дня.

Собирается из того же материала, что и `/today`: просрочка, записи на сегодня.
`build_day` — единственное место, где день превращается в текст; `views.cmd_today`
зовёт её же, иначе два вывода одного и того же разойдутся при первой правке.

Блоков четыре, и порядок у них не косметический: просрочка, день, «Дальше»,
«Без срока». Каждый следующий тратит то, что осталось от бюджета символов, —
поэтому первым идёт то, что важнее, а обрезается всегда справочный хвост, а не
сегодняшние дела.
"""

import logging
from collections.abc import Sequence
from datetime import date, datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Entry, Family
from bot.services import review, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)


def _block(
    head: str,
    entries: Sequence[Entry],
    tz: str,
    now: datetime,
    *,
    limit: int,
    budget: int,
    extra: int = 0,
) -> str | None:
    """Заголовок плюс строки, либо `None`, если показывать нечего.

    Общий сборщик появился на этапе 10, когда блоков стало четыре. Дело не в
    краткости, а в ловушке `texts.entry_lines`: при нехватке бюджета та
    возвращает не пустой список, а `["…и ещё N"]` — хвост дописывается
    безусловно. Наивная проверка «строки непусты» нарисовала бы «➡️ Дальше» и
    под ним один счётчик, без единой настоящей записи.

    `extra` — сколько записей осталось за пределами переданного среза. Нужен
    блоку «Без срока»: там режет SQL, и `entry_lines` о доборе знать неоткуда.
    """
    if not entries:
        return None
    lines = texts.entry_lines(
        entries, tz, now, limit=limit, budget=budget - len(head) - 1
    )
    if not [line for line in lines if not line.startswith("…")]:
        return None
    if extra > 0 and not lines[-1].startswith("…"):
        lines = [*lines, texts.MORE_ITEMS.format(count=extra)]
    return head + "\n" + "\n".join(lines)


async def build_day(
    session: AsyncSession, family: Family, now: datetime | None = None
) -> tuple[str, bool]:
    """Текст дня и признак «есть что показывать».

    Признак нужен дайджесту: рассылать каждое утро «на сегодня ничего не
    запланировано» незачем. Команде `/today` он не нужен — там пустой ответ
    как раз уместен.
    """
    moment = now or tu.now_utc()
    today = tu.local_today(family.tz, moment)
    start, end = tu.day_bounds(today, family.tz)

    entries = await repo.entries_for_range(session, family.id, start, end)
    overdue = await repo.overdue_entries(session, family.id, start)

    # Оба списка обрезаны по MAX_DAY_ITEMS: просрочка копится годами, и без
    # потолка сводка однажды перерастает 4096 символов. Telegram отвечает
    # отказом, `sending.deliver` возвращает BROKEN, а `last_digest_on` всё равно
    # проставляется — дайджест пропал бы молча и навсегда.
    #
    # Счёта по записям для этого мало: длину заголовка задаёт человек, и
    # пятнадцать записей по 500 символов дают 7500. Поэтому у `entry_lines` есть
    # второй потолок, по символам, и бюджет тратится по ходу сборки —
    # просрочка идёт первой и берёт своё, дню достаётся остаток
    budget = texts.MESSAGE_LIMIT - texts.DAY_RESERVE

    blocks: list[str] = []

    def spend(block: str) -> None:
        nonlocal budget
        blocks.append(block)
        budget -= len(block) + 2  # блоки склеиваются через пустую строку

    overdue_block = _block(
        texts.HEADER_OVERDUE,
        overdue,
        family.tz,
        moment,
        limit=texts.MAX_DAY_ITEMS,
        budget=budget,
    )
    if overdue_block:
        spend(overdue_block)

    day_head = texts.day_header(today, family.tz, moment)
    body = "\n".join(
        texts.entry_lines(
            entries,
            family.tz,
            moment,
            limit=texts.MAX_DAY_ITEMS,
            show_date=False,
            budget=budget - len(day_head) - 1,
        )
    )
    # Пустой день при непустой просрочке говорит другое: «ничего не
    # запланировано» сразу под списком просроченного читается как противоречие
    empty = texts.EMPTY_TODAY_AFTER_OVERDUE if overdue else texts.EMPTY_TODAY
    # Бюджет тратится и здесь. До этапа 10 день был последним блоком, и вычитать
    # после него было незачем; теперь за ним идут ещё два, и без этой строки они
    # считали бы бюджет так, будто дня в сообщении нет
    spend(day_head + "\n" + (body or empty))

    # Ближайшее из будущего: до этапа 10 завтрашнее дело было видно только на
    # своей странице, и человек с пустым сегодня не знал, что назавтра звонок
    ahead = await repo.upcoming_entries(
        session, family.id, end, limit=texts.MAX_NEXT_ITEMS
    )
    next_block = _block(
        texts.HEADER_NEXT, ahead, family.tz, moment, limit=len(ahead), budget=budget
    )
    if next_block:
        spend(next_block)

    undated, undated_total = await repo.undated_entries(
        session, family.id, limit=texts.MAX_UNDATED_ITEMS
    )
    # Хвост «…и ещё N» здесь считается от общего числа, а не от показанного
    # среза: режет SQL, и `entry_lines` о доборе знать неоткуда
    undated_block = _block(
        texts.HEADER_UNDATED,
        undated,
        family.tz,
        moment,
        limit=texts.MAX_UNDATED_ITEMS,
        budget=budget,
        extra=undated_total - len(undated),
    )
    if undated_block:
        spend(undated_block)

    # Короткая сводка по покупкам для утреннего дайджеста. Именно
    # счётчик, а не список: пункты покупок не привязаны ко дню, и вываливать
    # тридцать строк в каждый `/today` незачем — за ними есть своя панель.
    #
    # На `has_content` она намеренно не влияет. Иначе утренняя сводка уходила бы
    # каждый день, пока в списке лежит хоть один непокупленный пункт, и «день
    # пуст» перестало бы означать «сегодня ничего нет».
    lst = await repo.active_list(session, family.id)
    if lst is not None:
        left = sum(
            1
            for item in await repo.list_items(session, lst.id)
            if item.status == "open"
        )
        if left:
            blocks.append(texts.shopping_summary(lst.name, left))

    # Признак «есть что показывать» считают только просрочка и сам день —
    # новые блоки на него не влияют, как и счётчик покупок. Довод тот же и он
    # решающий: задача без срока не истекает никогда, а запись на будущий год
    # непуста всегда. Включи их сюда — и утренняя сводка пойдёт каждое утро до
    # конца времён, а «день пуст» перестанет значить «сегодня ничего нет»
    return "\n\n".join(blocks), bool(entries or overdue)


def is_due(family: Family, now: datetime) -> bool:
    """Пора ли слать дайджест: сегодня ещё не слали и время наступило."""
    today = tu.local_today(family.tz, now)
    if family.last_digest_on is not None and family.last_digest_on >= today:
        return False
    try:
        moment = tu.parse_hhmm(family.digest_time)
    except (ValueError, AttributeError):
        # Кривая строка в БД не должна останавливать дайджест остальным семьям
        log.warning("Семья #%s: непонятное digest_time %r", family.id, family.digest_time)
        return False
    return now >= tu.at_local_time(today, moment, family.tz)


async def send_pending(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Разослать дайджест тем семьям, кому пора."""
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        if not is_due(family, moment):
            continue
        try:
            await _send_one(bot, session, family, moment)
        except Exception:
            log.exception("Дайджест семьи #%s не отправлен", family.id)


async def _send_one(
    bot: Bot, session: AsyncSession, family: Family, now: datetime
) -> None:
    today = tu.local_today(family.tz, now)
    text, has_content = await build_day(session, family, now)

    if has_content:
        blocks = [texts.DIGEST_HEADER]
        if _is_late(family, today, now):
            blocks.append(texts.DIGEST_LATE_NOTE)
        blocks.append(text)
        if await sending.deliver(bot, family, "\n\n".join(blocks)) == sending.RETRY:
            return  # сеть или флуд — попробуем на следующем тике
        if await _send_review(bot, session, family, now) == sending.RETRY:
            return  # день не помечаем: на следующем тике уйдут оба сообщения

    # Отметка ставится и на пустом дне: иначе каждый тик до полуночи будет
    # заново собирать сводку, которую всё равно не отправит
    await repo.set_last_digest_on(session, family, today)


async def _send_review(
    bot: Bot, session: AsyncSession, family: Family, now: datetime
) -> str:
    """Разбор незакрытого вторым сообщением — только если есть что разбирать.

    Молчание на пустом списке важнее краткости: «разбирать нечего» каждое утро
    — это шум, от которого сводку начинают пролистывать не читая.
    """
    entries = await review.overdue(session, family, now)
    text, shown = review.render(entries, family.tz, now)
    if not shown:
        return sending.OK  # разбирать нечего — молчим
    return await sending.deliver(
        bot, family, text, reply_markup=kb.review_keyboard(shown)
    )


def _is_late(family: Family, today: date, now: datetime) -> bool:
    planned = tu.at_local_time(today, tu.parse_hhmm(family.digest_time), family.tz)
    return (now - planned).total_seconds() > settings.late_silent_min * 60
