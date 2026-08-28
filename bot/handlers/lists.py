"""Список покупок: одна живая панель с чекбоксами (этап 4).

Панель — сообщение с inline-кнопками, которое бот редактирует, а не шлёт
заново. `lists.panel_message_id` держит её id, поэтому она переживает
перезапуск бота: после рестарта `/buy сыр` правит ту же панель, а не выпускает
вторую.

**Дебаунса здесь нет, и это осознанно.** `panel.schedule` откладывает работу на
`PANEL_DEBOUNCE_SECONDS`, потому что панель дня перерисовывается от событий,
которых человек не ждёт. Тап по чекбоксу — наоборот, интерактив: две секунды
без зачёркивания читаются как «бот завис», человек тапает снова, и `schedule`
склеил бы обе правки в одну. Тап к тому же приходит вместе с `call.message`,
который можно отредактировать прямо в хендлере, — откладывать нечего.

Что из `panel.py` переиспользуется по существу — **дисциплина лока**. Гонка тут
не в SQLite (WAL и `busy_timeout` разведут две записи в разные строки), а в
рендере: хендлер A читает пункты до коммита B, а его правка доезжает до
Telegram после — и панель застревает в состоянии без пункта B до следующего
тапа. Лок вокруг блока «перечитать → отрисовать → отредактировать» это чинит.

Ключ лока — `list_id`, а не `family_id`: у панели дня свой словарь в `panel.py`,
и общий ключ заставил бы их отменять работу друг друга.

Оговорка про порядок: в `panel.refresh` лок берётся **до** сессии, здесь
наоборот — сессию открыл `FamilyMiddleware` ещё до входа в хендлер, и иначе
никак. Это безопасно: простаивающая `AsyncSession` в режиме WAL блокировок не
держит, писательская берётся только на время коммита, а он случается до похода
в Telegram.
"""

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db import repo
from bot.db.models import Entry, Family, ListModel, Member
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.handlers.views import edit_or_ignore
from bot.services import panel, sending

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

# Восемь кнопок в ряд — предел, при котором подпись «☐ 10» ещё не сжимается на
# телефоне. При потолке `MAX_LIST_ITEMS` это четыре ряда
BUTTONS_PER_ROW = 8

_locks: dict[int, asyncio.Lock] = {}


class ListCB(CallbackData, prefix="buy"):
    """`target` — `entry_id` для «tick» и `list_id` для «close».

    Класс живёт здесь, а не в `keyboards.py`, по образцу мастера `/new`: тот
    тоже держит свои клавиатуры у себя. Клавиатура списка нужна ровно одному
    модулю, а `keyboards.py` — общий файл, и лишний повод в него ходить дороже
    формальной симметрии с `PageCB` / `DoneCB`.
    """

    action: str  # 'tick' | 'close'
    target: int


def _split_titles(raw: str) -> list[str]:
    """«молоко, хлеб» и переводы строк — в отдельные пункты.

    Заголовок режется здесь, на сыром вводе, а не при рендере: обрезать уже
    экранированный текст нельзя (рвёт «&lt;» пополам), а пункт, не влезающий в
    панель, остаётся без кнопки — то есть его нечем вычеркнуть, и список
    никогда не закроется.
    """
    parts = [chunk.strip() for line in raw.splitlines() for chunk in line.split(",")]
    return [p[: texts.MAX_ITEM_TITLE] for p in parts if p]


def _keyboard(items: list[Entry], lst: ListModel) -> InlineKeyboardMarkup | None:
    """Кнопки нумерованы, а не подписаны названиями.

    Зачеркнуть текст на кнопке Telegram не даёт — форматирования у подписи нет,
    так что состояние всё равно живёт в тексте сообщения. Тридцать кнопок с
    названиями — стена на два экрана, тридцать номеров — четыре ряда. Тот же
    приём и тот же инвариант, что у `keyboards.entry_list_keyboard`: **кнопок
    ровно столько, сколько пронумерованных строк**, иначе тап уйдёт не туда.
    """
    if not items:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, entry in enumerate(items, start=1):
        icon = "✅" if entry.status == "done" else "☐"
        row.append(
            InlineKeyboardButton(
                text=f"{icon} {i}",
                callback_data=ListCB(action="tick", target=entry.id).pack(),
            )
        )
        if len(row) == BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # У закрытого списка нижняя кнопка меняется на обратную. Без неё список,
    # закрытый с непокупленным остатком, недостижим: из `/buy` он ушёл, а тап
    # по пункту его не оживит — оживляет только снятая галка
    if lst.archived:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.BTN_REOPEN_LIST,
                    callback_data=ListCB(action="reopen", target=lst.id).pack(),
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.BTN_CLOSE_LIST,
                    callback_data=ListCB(action="close", target=lst.id).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _state(session: AsyncSession, lst: ListModel) -> list[Entry]:
    """Пункты списка плюс пересчёт признака «список закрыт»."""
    items = await repo.list_items(session, lst.id)
    await repo.sync_list_archived(session, lst)
    return items


def _render(
    family: Family, lst: ListModel, items: list[Entry]
) -> tuple[str, InlineKeyboardMarkup | None]:
    text, shown = texts.shopping_panel(
        lst.name, items, family.tz, closed=lst.archived
    )
    return text, _keyboard(items[:shown], lst)


async def _show(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    lst: ListModel,
    message: Message,
    *,
    force_new: bool,
) -> None:
    """Показать панель: править существующую или выпустить новую.

    Правка на месте — то, ради чего заведена колонка `panel_message_id`: пока
    семья докидывает пункты в магазине, панель обязана оставаться одним
    сообщением, а не расти стопкой. Уехавшую вверх панель редактировать
    бессмысленно — её не видно, — и порог тут тот же `panel_max_messages`, что
    у панели дня; своей настройки не заводим.

    Закрепления у панели списка нет намеренно. Видимость ей даёт перевыпуск по
    `panel_max_messages`, а второй закреплённый в чате и вторая зависимость от
    прав администратора пользы не добавляют — владелец 27.08 отдельно сказал,
    что закреп второстепенен.
    """
    items = await _state(session, lst)
    text, markup = _render(family, lst, items)

    near = (
        lst.panel_message_id is not None
        and message.message_id - lst.panel_message_id <= settings.panel_max_messages
    )
    if not force_new and near:
        status = await sending.edit(bot, family, lst.panel_message_id, text, markup)
        if status in (sending.OK, sending.RETRY, sending.BROKEN):
            return
        # Осталось NOT_FOUND (панель удалили руками) и FORBIDDEN — им нужна новая

    sent = await message.answer(text, reply_markup=markup)
    await repo.set_list_panel(session, lst, sent.message_id)


async def refresh_panel(
    bot: Bot,
    session: AsyncSession,
    family: Family,
    message: Message,
) -> None:
    """Перерисовать панель списка, когда пункт положили мимо этого роутера.

    Покупка приходит не только из `/buy`: её кладут разбор «+» (`capture`) и
    мастер `/new` — с шага 4.5 оба зовут `repo.shopping_slot`. Без этого вызова
    пункт в базе есть, а панель в чате показывает прежнее содержимое до
    ближайшего тапа или `/buy`. Расходятся не данные, а картинка — но живая
    панель ради картинки и заведена.

    Панель, которой ещё нет, здесь не выпускается: человек попросил записать
    покупку, а не показать список, и второе сообщение в ответ на одну фразу
    было бы навязчивым. Первую панель по-прежнему выпускает `/buy`.
    """
    lst = await repo.active_list(session, family.id)
    if lst is None or lst.panel_message_id is None:
        return
    # Лок по той же причине, что и в `tick`: между «перечитать» и
    # «отредактировать» может влезть чужой тап, и в чат уедет правка по
    # устаревшему чтению. Ключ тот же — `list_id`
    async with _locks.setdefault(lst.id, asyncio.Lock()):
        await _show(bot, session, family, lst, message, force_new=False)


async def _open(
    message: Message,
    raw: str,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    titles = _split_titles(raw)

    if not titles:
        lst = await repo.active_list(session, family.id)
        if lst is None:
            # Активного нет — но закрытый список с непокупленным остатком
            # показать обязаны, иначе его пункты пропадают бесследно
            lst = await repo.closed_list_with_leftovers(session, family.id)
        if lst is None:
            # Пустую строку в `lists` здесь не заводим. Дело не в аккуратности:
            # `repo._family_is_empty` считает списки, и семья со строкой в
            # `lists` перестаёт считаться пустышкой — а на этой проверке держится
            # переезд в супергруппу, где второго шанса не будет
            await message.answer(texts.BUY_USAGE, reply_markup=kb.main_keyboard())
            return
        # Человек попросил показать список — значит должен его увидеть, а не
        # получить правку сообщения, уехавшего вверх по истории
        await _show(bot, session, family, lst, message, force_new=True)
        return

    lst = await repo.get_or_create_active_list(session, family.id)
    room = texts.MAX_LIST_ITEMS - len(await repo.list_items(session, lst.id))
    if room <= 0:
        await message.answer(texts.list_full(), reply_markup=kb.main_keyboard())
        return

    # Про отброшенные говорим вслух. Набрал пять, увидел два — без объяснения
    # это читается как «бот проглотил», и человек набирает их заново
    if len(titles) > room:
        await message.answer(texts.list_partial(room, len(titles) - room))

    await repo.add_items(
        session,
        family_id=family.id,
        author_id=member.id,
        list_id=lst.id,
        titles=titles[:room],
    )
    await _show(bot, session, family, lst, message, force_new=False)
    panel.schedule(bot, family.id, message.message_id)


@router.message(Command("buy"))
async def cmd_buy(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    await _open(message, command.args or "", session, family, member, bot)


@router.message(F.text == kb.BTN_BUY)
async def btn_buy(
    message: Message,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """Кнопка нижней клавиатуры — тот же путь, но без аргументов."""
    await _open(message, "", session, family, member, bot)


@router.callback_query(ListCB.filter(F.action == "tick"))
async def tick(
    call: CallbackQuery,
    callback_data: ListCB,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """Переключить пункт и перерисовать панель на месте.

    Запись ищется по `entry_id` из колбэка, а не «в активном списке», — поэтому
    панель уже закрытого списка остаётся рабочей: промах пальцем в магазине
    отменяется тем же движением, каким сделан.

    Оживляет список **только снятие галки**. Тап в другую сторону (купили ещё
    один пункт) закрытый список не открывает: если человек закрыл его кнопкой,
    недокупленный остаток — это его решение, а не недосмотр.
    """
    entry = await repo.toggle_bought(
        session, callback_data.target, family.id, member.id
    )
    if entry is None:
        await call.answer(texts.LIST_ITEM_GONE, show_alert=True)
        return

    lst = await repo.list_with_panel(session, family.id, entry.list_id)
    if lst is None:
        await call.answer(texts.LIST_ITEM_GONE, show_alert=True)
        return

    if entry.status == "open":
        await repo.reopen_list(session, lst)

    # Без текста: зачёркивание в панели и есть обратная связь
    await call.answer()
    # В панели дня есть счётчик покупок — она обязана узнать об изменении.
    # Здесь `schedule` уместен именно потому, что панель дня фоновая: серия
    # тапов в магазине склеится дебаунсом в одну её правку
    panel.schedule(bot, family.id, call.message.message_id)
    async with _locks.setdefault(lst.id, asyncio.Lock()):
        items = await _state(session, lst)
        text, markup = _render(family, lst, items)
        await edit_or_ignore(call, text, markup)


@router.callback_query(ListCB.filter(F.action == "close"))
async def close_list(
    call: CallbackQuery,
    callback_data: ListCB,
    session: AsyncSession,
    family: Family,
    bot: Bot,
) -> None:
    """Явно закрыть список, не вычёркивая остаток.

    Нужна для «сметаны не было, а мы уже дома» и как единственный выход у
    списка, упёршегося в `MAX_LIST_ITEMS`.
    """
    lst = await repo.list_with_panel(session, family.id, callback_data.target)
    if lst is None:
        await call.answer(texts.LIST_ITEM_GONE, show_alert=True)
        return

    await repo.close_list(session, lst)
    await call.answer(texts.LIST_CLOSED)
    panel.schedule(bot, family.id, call.message.message_id)
    async with _locks.setdefault(lst.id, asyncio.Lock()):
        items = await repo.list_items(session, lst.id)
        text, markup = _render(family, lst, items)
        await edit_or_ignore(call, text, markup)


@router.callback_query(ListCB.filter(F.action == "reopen"))
async def reopen(
    call: CallbackQuery,
    callback_data: ListCB,
    session: AsyncSession,
    family: Family,
    bot: Bot,
) -> None:
    """Вернуть закрытый список в работу.

    Обратная сторона «🧹 Список закрыт». Закрытие липкое (иначе его отменял бы
    первый же тап по остатку), поэтому отменять его должно что-то явное — иначе
    непокупленные пункты становятся недостижимы вовсе.
    """
    lst = await repo.list_with_panel(session, family.id, callback_data.target)
    if lst is None:
        await call.answer(texts.LIST_ITEM_GONE, show_alert=True)
        return

    await repo.reopen_list(session, lst)
    await call.answer(texts.LIST_REOPENED)
    panel.schedule(bot, family.id, call.message.message_id)
    async with _locks.setdefault(lst.id, asyncio.Lock()):
        items = await repo.list_items(session, lst.id)
        text, markup = _render(family, lst, items)
        await edit_or_ignore(call, text, markup)
