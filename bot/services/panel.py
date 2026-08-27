"""Живая панель дня: одно закреплённое «📌 Сегодня» на семью.

Панель — не ещё один вывод дня, а тот же самый: текст собирает
`digest.build_day`, как для `/today` и утренней сводки. Разница в том, что
сообщение одно и то же — бот его редактирует, а не шлёт новое (`PLAN.md`, п. 3).

Сессию хендлера сюда передавать нельзя. `schedule` откладывает работу на
`PANEL_DEBOUNCE_SECONDS`, а `FamilyMiddleware` закроет свою сессию сразу по
выходу из хендлера — то есть за секунды до пробуждения задачи. Плюс
`AsyncSession` не рассчитан на конкурентное использование двумя задачами.
Поэтому `refresh` открывает сессию сам, как и остальные фоновые сервисы.

`Session` импортируется по имени на уровне модуля намеренно: иначе тест не
сможет подменить фабрику через `monkeypatch.setattr(panel, "Session", ...)`.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Family
from bot.db.session import Session
from bot.services import digest, sending
from bot.services import timeutil as tu

log = logging.getLogger(__name__)

# Ключ везде — family_id. Словари растут ровно по числу семей, чистить незачем
_locks: dict[int, asyncio.Lock] = {}
_tasks: dict[int, asyncio.Task] = {}


def schedule(bot: Bot, family_id: int, last_message_id: int | None = None) -> None:
    """Поставить перерисовку в очередь и сразу вернуть управление.

    Синхронная намеренно: хендлер не должен ждать панель — ответ человеку
    важнее. Предыдущая невыполненная задача отменяется, в этом и состоит
    дебаунс: из серии правок подряд до Telegram доживает только последняя.

    Словарь `_tasks` заодно держит сильную ссылку на задачу — без неё сборщик
    мусора вправе собрать её раньше, чем она отработает.
    """
    previous = _tasks.get(family_id)
    if previous is not None:
        previous.cancel()
    _tasks[family_id] = asyncio.create_task(
        _debounced(bot, family_id, last_message_id), name=f"panel:{family_id}"
    )


async def _debounced(bot: Bot, family_id: int, last_message_id: int | None) -> None:
    await asyncio.sleep(settings.panel_debounce_seconds)
    # Дальше отменять нечего: работа началась, и обрыв посреди правки оставил бы
    # панель рассинхронизированной с базой. У следующего `schedule` будет своя
    # задача, а лок выстроит их в очередь
    _tasks.pop(family_id, None)
    try:
        await refresh(bot, family_id, last_message_id)
    except Exception:
        # Задача fire-and-forget: без перехвата исключение всплыло бы как
        # «Task exception was never retrieved» уже после её смерти
        log.exception("Панель семьи #%s не обновлена", family_id)


async def refresh(
    bot: Bot,
    family_id: int,
    last_message_id: int | None = None,
    now: datetime | None = None,
) -> None:
    """Пересобрать панель семьи: отредактировать или выпустить новую.

    `last_message_id` — самое свежее сообщение чата, какое мы видели. По нему
    считается, далеко ли уехала панель.
    """
    # Сначала лок, потом сессия: стоять в очереди, держа соединение SQLite,
    # незачем — тикер пишет в ту же базу
    async with _locks.setdefault(family_id, asyncio.Lock()):
        async with Session() as session:
            family = await repo.get_family_by_id(session, family_id)
            if family is None:
                return
            await _redraw(bot, session, family, last_message_id, now or tu.now_utc())


async def refresh_stale(
    bot: Bot, session: AsyncSession, now: datetime | None = None
) -> None:
    """Перевыпустить панели, оставшиеся за вчерашний день. Зовёт тикер.

    В обычный тик не делает ни одного обращения к Telegram — только читает
    семьи. Отправка случается ровно один раз за локальные сутки.

    Семьям без панели её здесь не заводим: панель появляется от первой же
    записи, а навязывать закреплённое сообщение чату, который ничего не
    планирует, незачем.
    """
    moment = now or tu.now_utc()
    for family in await repo.all_families(session):
        if family.panel_message_id is None:
            continue
        if family.panel_day == tu.local_today(family.tz, moment):
            continue
        # Именно schedule, а не await refresh: у тикера открыта своя сессия, и
        # вложенная транзакция к SQLite — прямой путь к «database is locked»
        schedule(bot, family.id)


async def shutdown() -> None:
    """Погасить незавершённые дебаунсы при остановке бота.

    Без этого задача проснётся уже после `bot.session.close()` и на каждой
    остановке будет писать в лог сетевую ошибку.
    """
    tasks = list(_tasks.values())
    _tasks.clear()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


async def _redraw(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    last_message_id: int | None,
    now: datetime,
) -> None:
    # Признак «есть что показывать» игнорируем намеренно: панель — постоянный
    # элемент чата, «На сегодня ничего не запланировано» в ней уместно. Это
    # дайджест на пустом дне молчит, а панель — нет
    body, _ = await digest.build_day(session, family, now)
    text = texts.panel(body)

    if family.panel_message_id is not None and not _outdated(family, now, last_message_id):
        status = await sending.edit(bot, family, family.panel_message_id, text)
        if status in (sending.OK, sending.RETRY, sending.BROKEN):
            # Единственный внешний признак, что дебаунс сработал: на серию
            # правок подряд в логе обязана быть одна строка, а не по строке
            # на каждую запись
            log.info(
                "Панель #%s: правка, %s", family.panel_message_id, status
            )
            return
        # Осталось NOT_FOUND (панель удалили руками) и FORBIDDEN (выгнали) —
        # оба разбирает `_publish`

    await _publish(bot, session, family, text, now)


def _outdated(family: Family, now: datetime, last_message_id: int | None) -> bool:
    """Панель невидима: уехала вверх по истории или осталась за вчера.

    `last_message_id` — оценка снизу: тап по inline-кнопке сообщений в чат не
    добавляет, а `call.message` может быть старым списком. Недооценка лишь
    откладывает перевыпуск до следующей правки, переоценки не бывает.
    """
    if family.panel_day != tu.local_today(family.tz, now):
        return True
    if last_message_id is None or family.panel_message_id is None:
        return False
    return last_message_id - family.panel_message_id > settings.panel_max_messages


async def _publish(
    bot: Bot, session: AsyncSession, family: Family, text: str, now: datetime
) -> None:
    previous = family.panel_message_id
    status, message_id = await sending.send(bot, family, text, silent=True)
    if status != sending.OK or message_id is None:
        if status == sending.FORBIDDEN and previous is not None:
            # Бота выгнали: старый id стал мусором. Вернут в чат — заведём новую
            await repo.set_panel(session, family, None, None)
        elif status == sending.BROKEN and previous is not None:
            # Текст не принимают (скорее всего перерос 4096) — повтор даст ровно
            # ту же ошибку. Помечаем день отработанным, иначе тикер, видя
            # вчерашний `panel_day`, будет слать заведомо битое каждую минуту
            # до полуночи. Панель остаётся вчерашней; починится завтра
            await repo.set_panel(
                session, family, previous, tu.local_today(family.tz, now)
            )
        # RETRY здесь не помечаем: сеть и флуд-контроль проходят, и следующий
        # тик обязан попробовать снова
        return

    # Пишем в базу ДО закрепления. Если прав админа нет, панель обязана остаться
    # незакреплённой, а не выпускаться заново на каждой правке
    await repo.set_panel(session, family, message_id, tu.local_today(family.tz, now))
    log.info("Панель выпущена заново: #%s вместо #%s", message_id, previous)
    if previous is not None:
        await _pin(bot, family, previous, pin=False)
    await _pin(bot, family, message_id, pin=True)


async def _pin(bot: Bot, family: Family, message_id: int, *, pin: bool) -> None:
    """Закрепление — украшение: без прав админа панель просто живёт внизу чата.

    Ловим всё подряд намеренно. «not enough rights to pin a message»,
    «message to unpin not found», сетевой сбой — различать их здесь незачем,
    реакция одна: записать в лог и работать дальше.
    """
    try:
        if pin:
            await bot.pin_chat_message(
                family.chat_id, message_id, disable_notification=True
            )
        else:
            # message_id только по ключу: позиционно он уйдёт в
            # business_connection_id (сигнатура aiogram 3.31)
            await bot.unpin_chat_message(family.chat_id, message_id=message_id)
    except Exception:
        log.info(
            "Панель в чате %s осталась незакреплённой", family.chat_id, exc_info=True
        )
