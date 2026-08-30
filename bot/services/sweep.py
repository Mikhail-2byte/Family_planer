"""Утренняя уборка чата (этап 11).

Каждое утро, сразу за сводкой, бот стирает всё, что лежит в чате выше неё, —
чтобы день начинался с чистого листа, а сводка была первым сообщением.

**Удаляем диапазоном id, а не списком запомненных, и это ключевое решение.**
Бот не умеет читать историю чата: удалить он может только то, чей `message_id`
знает, а знает он ровно два — панель дня и панель списка. Запоминать каждое
отправленное значило бы завести middleware на сессию бота и новую таблицу ради
73 мест отправки. Но `message_id` в чате — сплошная возрастающая нумерация, а
`deleteMessages` молча пропускает несуществующие id, поэтому достаточно идти
подряд.

Побочная выгода, которой у уборки по списку не было бы вовсе: сотрутся и
сообщения, которых бот **никогда не видел**. На старте он делает
`delete_webhook(drop_pending_updates=True)`, то есть всё пришедшее, пока он
лежал, до него не доходит — и по списку запомненных осталось бы в чате навсегда.

Якорь — id только что отправленной сводки. Другого способа узнать текущий
максимум id в чате нет, поэтому порядок физически может быть только «сводка →
уборка». Разница в доли секунды, вид тот же, а сорвавшаяся уборка оставляет чат
со сводкой и историей, а не пустой чат без сводки.

Модуль отдельный, а не кусок `digest.py`: тот про то, как день превращается в
текст. Наружу исключений не выпускает — политика `backup.run_daily` и
`parse_log.write`: грязный чат на одно утро дешевле упавшего дайджеста.
"""

import asyncio
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import Family
from bot.services import sending

log = logging.getLogger(__name__)

# Столько id принимает `deleteMessages` за раз — потолок Telegram, не наш
BATCH = 100
# Сколько одиночных удалений позволяем себе за утро, когда пачка упала.
# Без потолка тысяча неудаляемых сообщений дала бы тысячу запросов подряд
MAX_SINGLE = 100
# Пауза между одиночными: сотня запросов без неё упирается во флуд-контроль
SINGLE_PAUSE = 0.1


def plan(
    swept_upto: int | None, anchor: int, window: int, batch: int = BATCH
) -> list[list[int]]:
    """Диапазон id к удалению, нарезанный на пачки. Чистая функция.

    Границы: снизу — то, что уже вычищено, но не дальше `window` назад; сверху —
    `anchor - 1`, то есть сама сводка и всё, что пришло после неё, остаётся.

    `window` служит сразу двум целям, и второй неочевиден. Первая — первый
    запуск: без потолка бот полез бы удалять с первого id чата. Вторая —
    догонка после простоя: ноутбук может проспать двое суток, и без потолка
    диапазон рос бы неограниченно. С ним безнадёжно старое отваливается за одно
    утро и больше не трогается.

    Клампинг к единице обязателен: у молодого чата `anchor` бывает меньше окна,
    и `range` ушёл бы в нулевые и отрицательные id.

    `window == 0` выключает уборку целиком — идиома проекта (`BACKUP_KEEP=0`,
    `LLM_DAILY_LIMIT=0`).
    """
    if window <= 0 or anchor <= 1:
        return []

    start = max(1, anchor - window, (swept_upto or 0) + 1)
    ids = list(range(start, anchor))
    return [ids[i : i + batch] for i in range(0, len(ids), batch)]


async def run(
    bot: Bot, session: AsyncSession, family: Family, anchor: int
) -> None:
    """Вычистить чат выше `anchor`. Наружу не выпускает ничего.

    Водяной знак двигается по **фактически пройденным** пачкам, а не по
    задуманному диапазону: оборванная уборка обязана оставить остаток
    завтрашнему утру, а не считать его сделанным.
    """
    try:
        await _run(bot, session, family, anchor)
    except Exception:
        log.exception("Уборка чата %s сорвалась", family.chat_id)


async def _run(
    bot: Bot, session: AsyncSession, family: Family, anchor: int
) -> None:
    batches = plan(family.swept_upto, anchor, settings.sweep_window)
    if not batches:
        return

    budget = MAX_SINGLE
    done_upto: int | None = None

    for chunk in batches:
        status = await sending.delete_batch(bot, family, chunk)

        if status == sending.FORBIDDEN:
            # Прав нет — поштучно будет ровно то же самое, только сто раз
            log.warning("Уборка чата %s остановлена: нет прав", family.chat_id)
            break
        if status == sending.RETRY:
            # Сеть или флуд: остаток достаётся завтрашнему диапазону
            break

        if status == sending.OK:
            # Пачка ушла целиком — двигаем знак за её хвост
            done_upto = chunk[-1]
            continue

        # BROKEN: в пачке есть неудаляемое. Какое именно, Telegram не говорит,
        # поэтому проходим её поштучно — остальные удалить всё равно надо
        reached, budget, halted = await _one_by_one(bot, family, chunk, budget)
        if reached is not None:
            done_upto = reached
        if halted:
            break

    if done_upto is None:
        return

    await repo.set_swept_upto(session, family, done_upto)
    await _forget_panel(session, family, done_upto)


async def _one_by_one(
    bot: Bot, family: Family, chunk: list[int], budget: int
) -> tuple[int | None, int, bool]:
    """Разобрать упавшую пачку поштучно. Отдаёт `(докуда дошли, остаток, стоп)`.

    Отдаёт именно **последний тронутый id**, а не хвост пачки, и это главное
    здесь. Знак двигается только за тем, что реально пытались удалить: иначе
    исчерпанный бюджет объявлял бы вычищенными сотни сообщений, которых никто
    не трогал, — и они остались бы в чате навсегда, потому что завтрашний
    диапазон начался бы уже за ними. На боевом чате из 235 сообщений это было
    135 штук.

    Бюджет общий на всё утро: тысяча неудаляемых не должна дать тысячу запросов
    подряд. Кончился — останавливаем уборку целиком, остаток достаётся
    завтрашнему диапазону.

    `BROKEN` на отдельном сообщении — не беда, а ожидаемый исход: это и есть то
    самое неудаляемое, ради которого затевался откат. Идём дальше по пачке.
    """
    done: int | None = None
    for message_id in chunk:
        if budget <= 0:
            log.info(
                "Уборка чата %s: бюджет поштучных исчерпан, остаток — завтра",
                family.chat_id,
            )
            return done, budget, True
        budget -= 1
        status = await sending.delete_one(bot, family, message_id)
        if status in (sending.FORBIDDEN, sending.RETRY):
            return done, budget, True
        done = message_id
        await asyncio.sleep(SINGLE_PAUSE)
    return done, budget, False


async def _forget_panel(
    session: AsyncSession, family: Family, done_upto: int
) -> None:
    """Пометить панель дня устаревшей, если её снесло уборкой.

    Сама панель вернётся в этом же тике: `ticker.tick_once` зовёт
    `panel.refresh_stale` следом за дайджестом, а та перевыпускает панель,
    у которой `panel_day` не сегодняшний.

    Почему нельзя обойтись без этого. `refresh_stale` считает панель свежей по
    `panel_day`, и если кто-то завёл запись в 07:00, панель уже перевыпущена
    сегодняшним днём. Уборка её сотрёт, а `refresh_stale` сочтёт живой — и
    панели не будет весь день.

    Почему нельзя обнулить `panel_message_id`: `refresh_stale` пропускает семьи
    без панели, и результат тот же — сутки без неё.

    Идём через `repo.set_panel`, а не присваиванием: инвариант «панель и её день
    меняются только вместе» остаётся буквально соблюдён, а состояние «id есть,
    дня нет» — это ровно «панель осталась за вчера», которое `_outdated` уже
    умеет читать.

    Панель **списка покупок** здесь намеренно не трогается: `lists.refresh_panel`
    выходит молча при пустом `panel_message_id`, и обнуление стоило бы пункта,
    добавленного разбором, — он лёг бы в базу, не показавшись в чате. Мёртвый id
    самолечится сам: правка вернёт `NOT_FOUND`, и панель выпустится заново.
    """
    if family.panel_message_id is None or family.panel_message_id > done_upto:
        return
    await repo.set_panel(session, family, family.panel_message_id, None)
