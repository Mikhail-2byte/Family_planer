"""Доводка по итогам живого прогона (этап 10, 30.08.2026).

Третий файл регрессий, и повод у него свой. `test_regressions.py` — ревизия
этапов 0–1, `test_regressions_audit.py` — сплошная ревизия 0–7 по коду. Здесь
другое: бот отработал в семейном чате, и владелец вернулся с претензиями,
которые из чтения кода не видны — значок читался наоборот, заметка не
отличалась от задачи, купленный список продолжал занимать экран. Смешивать это
с ревизией по коду не надо, иначе через полгода не понять, какой тест каким
наблюдением вызван.

Правило то же, что у соседей: **каждый тест обязан падать на коде «до правки»**.
Проверено прогоном на `git worktree` с кодом этапа 9 — 22 из 24 красные.

Двое оставшихся — обратные половины, и они здесь намеренно. Правка бывает не
только неполной, но и слишком широкой: фильтр «заметки не просрочены» легко
написать так, что просрочка исчезнет вся, а сведение подписей типов — так, что
разъедутся уже они. Такой тест зелёный по обе стороны правки и краснеет, если
перестараться. Каждый из них говорит об этом в своём докстринге; третий такой
же сторож живёт в `test_capture_wiring.py`
(`test_legacy_tasks_button_still_opens_the_page`).
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.handlers.views import _render_page
from bot.services import timeutil as tu

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # четверг, 12:00 по Москве


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


class _FakeCall:
    """Колбэк с `answer` и `message.edit_text` — как в `test_lists.py`."""

    def __init__(self, bot, chat_id: int, message_id: int = 500):
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, object]] = []
        self.message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            edit_text=self._edit,
        )
        self.bot = bot

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def _edit(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))


# --- Значок состояния и значок вида — разные значки ---------------------------


@pytest.mark.asyncio
async def test_open_entry_is_an_empty_box_not_a_check_mark(session, family, anya):
    """Главная претензия живого прогона: «почему невыполненная задача с галочкой».

    До правки `KIND_ICONS["task"]` был «✅», и открытая задача выглядела
    закрытой. Теперь значков два: состояние и вид.
    """
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Позвонить Виктории", due_at=_msk(2026, 8, 28, 12, 0),
    )
    await session.refresh(entry, ["author"])

    line = texts.entry_line(entry, MSK, NOW)

    assert line.startswith(f"{texts.STATE_OPEN} {texts.KIND_ICONS['task']} ")
    assert not line.startswith(texts.STATE_DONE)


@pytest.mark.asyncio
async def test_done_entry_keeps_its_kind_and_gains_the_check(session, family, anya):
    """Закрытая запись меняет **состояние**, а не вид.

    До правки вид у неё подменялся на «✔️», и по строке нельзя было сказать,
    задача это была или заметка.
    """
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note", title="Мысль"
    )
    await repo.complete_entry(session, entry.id, family.id, anya.id)
    await session.refresh(entry, ["author", "closer"])

    line = texts.entry_line(entry, MSK, NOW)

    assert line.startswith(f"{texts.STATE_DONE} {texts.KIND_ICONS['note']} ")
    assert "✔️" not in line


def test_task_icon_is_not_the_done_icon():
    """Дешёвый сторож от отката: вид задачи не должен совпадать с «сделано».

    Совпадение и было болезнью — «✅» значил разом тип записи, кнопку закрытия,
    кнопку клавиатуры и «куплено».
    """
    assert texts.KIND_ICONS["task"] != texts.STATE_DONE
    assert texts.STATE_DONE not in texts.HEADER_TASKS
    assert texts.STATE_DONE not in kb.BTN_TASKS
    assert texts.STATE_DONE not in kb.BTN_SAVE


def test_wizard_and_capture_offer_the_same_kind_labels():
    """Подписи типов лежали двумя копиями и разошлись бы на первом же значке.

    Комментарий у `KIND_BUTTONS` при этом утверждал, что они общие.

    Сторож, а не регрессия: до правки копии совпадали построчно, и тест был
    зелёным. Краснеет он ровно тогда, когда их снова разведут.
    """
    from bot.handlers import new_entry

    wizard = [b.text for row in new_entry.KIND_KB.inline_keyboard for b in row]
    assert [text for text, _ in kb.KIND_BUTTONS] == wizard[: len(kb.KIND_BUTTONS)]


# --- Заметка — не задача с другой иконкой -------------------------------------


@pytest.mark.asyncio
async def test_note_with_a_past_due_is_never_overdue(session, family, anya):
    """Заметку нельзя «просрочить»: её надо помнить, а не сделать.

    До правки заметка с прошедшим сроком висела в «Просрочено» каждое утро и
    каждое утро попадала в разбор незакрытого, где ей предлагали «закрыть или
    перенести» — оба действия к факту неприменимы.
    """
    from bot.services import digest, review

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note",
        title="Дочь родится 3 сентября", due_at=_msk(2026, 8, 26, 10, 0),
    )
    now = _msk(2026, 8, 27, 9, 0)

    assert await repo.overdue_entries(session, family.id, now) == []
    assert await review.overdue(session, family, now) == []
    body, _ = await digest.build_day(session, family, now)
    assert texts.HEADER_OVERDUE not in body


@pytest.mark.asyncio
async def test_task_with_a_past_due_is_still_overdue(session, family, anya):
    """Обратная половина: фильтр по виду не должен глушить просрочку целиком.

    Сторож, а не регрессия: на коде «до правки» он зелёный — просрочка тогда
    брала всё подряд. Краснеет он от слишком широкой правки, а такую здесь
    написать легко: `overdue_entries` читают и сводка, и разбор незакрытого.
    """
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Оплатить садик", due_at=_msk(2026, 8, 26, 10, 0),
    )

    overdue = await repo.overdue_entries(session, family.id, _msk(2026, 8, 27, 9, 0))
    assert [e.title for e in overdue] == ["Оплатить садик"]


@pytest.mark.asyncio
async def test_notes_are_archived_not_completed(session, family, anya):
    """Заметку не «выполняют», её убирают с глаз — и кнопка обязана это говорить.

    Механизм у обеих один (`DoneCB` → `complete_entry`), и менять его нельзя:
    состав полей у кнопок, уже висящих в чате, трогать запрещено. Разница
    целиком в подписи — ровно так же, как в `DONE_CONFIRMED` / `NOTE_CLOSED`.
    """
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note", title="идея"
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="дело"
    )

    _, notes_kb = await _render_page(session, family, "notes", 0)
    _, tasks_kb = await _render_page(session, family, "tasks", 0)

    assert notes_kb.inline_keyboard[0][0].text == f"{kb.DONE_LABEL_BY_VIEW['notes']} 1"
    assert tasks_kb.inline_keyboard[0][0].text == f"{texts.STATE_DONE} 1"
    # Колбэк у обеих прежний: кнопки в чате обязаны продолжать работать
    assert notes_kb.inline_keyboard[0][0].callback_data.startswith("done:")


def test_prompt_tells_a_note_from_a_task():
    """Промпт различал их полуфразой «note — то, что просто нужно запомнить».

    Критерий не был операционализирован, и модель на пограничных фразах выбирала
    произвольно. Проверить можно только наличие правила — качество разбора
    видно лишь по `data/parse.log` живого чата.
    """
    from bot.services import parsing

    system = parsing.build_system(datetime(2026, 8, 27, 12, 0), MSK, ["Аня"], ["Покупки"])
    assert "помнить" in system
    assert "сделать" in system


# --- «Сегодня» перестаёт противоречить себе ----------------------------------


@pytest.mark.asyncio
async def test_empty_day_with_overdue_does_not_claim_nothing_is_planned(
    session, family, anya
):
    """«Просрочено: вчера» и сразу «на сегодня ничего не запланировано».

    Формально верно — окно суток пусто, — но человек читает сообщение целиком,
    и первое, что спросили на живом прогоне, было именно про это.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Ужин в ресторане", due_at=_msk(2026, 8, 26, 19, 0),
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.HEADER_OVERDUE in body
    assert texts.EMPTY_TODAY_AFTER_OVERDUE in body
    assert texts.EMPTY_TODAY not in body


@pytest.mark.asyncio
async def test_empty_day_without_overdue_keeps_the_old_wording(session, family):
    """Обратная половина: без просрочки прежняя фраза остаётся верной."""
    from bot.services import digest

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.EMPTY_TODAY in body
    assert texts.EMPTY_TODAY_AFTER_OVERDUE not in body


@pytest.mark.asyncio
async def test_tomorrow_is_visible_in_the_day(session, family, anya):
    """Завтрашнее дело было видно только на своей странице.

    `/today` берёт ровно окно суток, и «завтра в 12:00» в него не попадает —
    человек с пустым сегодня не знал, что назавтра его ждёт звонок.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Позвонить Виктории", due_at=_msk(2026, 8, 28, 12, 0),
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.HEADER_NEXT in body
    assert "Позвонить Виктории" in body


@pytest.mark.asyncio
async def test_today_entry_stays_in_the_day_not_in_the_next_block(
    session, family, anya
):
    """Граница блоков — конец сегодняшних суток, а не «сейчас»."""
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Забрать посылку", due_at=_msk(2026, 8, 27, 18, 0),
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.HEADER_NEXT not in body
    assert "Забрать посылку" in body


@pytest.mark.asyncio
async def test_entries_without_a_due_are_visible_at_all(session, family, anya):
    """Записанное без даты не показывалось ни в дне, ни в неделе.

    Оно жило только на своей странице, то есть пропадало из виду сразу после
    «Записал.» — при том что план дня человек открывает каждый день.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Разобрать балкон",
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="note",
        title="Пароль от вайфая",
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.HEADER_UNDATED in body
    assert "Разобрать балкон" in body and "Пароль от вайфая" in body


@pytest.mark.asyncio
async def test_shopping_never_leaks_into_the_undated_block(session, family, anya):
    """Главная ловушка блока «Без срока».

    Непротекание покупок в день держится на том, что у пунктов списка нет
    `due_at`, — а этот блок как раз выбирает записи без срока. Поэтому фильтр по
    виду обязан быть белым списком: с «всё, кроме shopping» следующий новый вид
    протёк бы молча.
    """
    from bot.services import digest

    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session, family_id=family.id, author_id=anya.id, list_id=lst.id,
        titles=["Молоко", "Хлеб"],
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Разобрать балкон",
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert texts.HEADER_UNDATED in body  # блок есть — значит проверка не холостая
    assert "Молоко" not in body and "Хлеб" not in body


@pytest.mark.asyncio
async def test_new_blocks_do_not_wake_the_morning_digest(session, family, anya):
    """Иначе сводка уходила бы каждое утро до конца времён.

    Задача без срока не истекает никогда, запись на будущий год непуста всегда.
    Тот же довод, по которому на признак не влияет счётчик покупок.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Разобрать балкон",
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Купить ёлку", due_at=_msk(2026, 12, 20, 12, 0),
    )

    body, has_content = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    assert has_content is False
    assert texts.HEADER_UNDATED in body and texts.HEADER_NEXT in body


@pytest.mark.asyncio
async def test_a_block_never_shows_a_header_over_a_bare_counter(session, family, anya):
    """`entry_lines` дописывает «…и ещё N» безусловно, даже когда строк ноль.

    Наивная проверка «строки непусты» рисовала бы «➡️ Дальше» и под ним один
    счётчик — заголовок без единой настоящей записи.
    """
    from bot.services import digest

    for i in range(3):
        await repo.create_entry(
            session, family_id=family.id, author_id=anya.id, kind="task",
            title=f"{i:02d} " + "молоко " * 70, due_at=_msk(2026, 9, 10, 12, 0),
        )

    block = digest._block(
        texts.HEADER_NEXT,
        await repo.upcoming_entries(
            session, family.id, _msk(2026, 8, 28), limit=texts.MAX_NEXT_ITEMS
        ),
        MSK,
        NOW,
        limit=texts.MAX_NEXT_ITEMS,
        budget=60,  # не хватит даже на одну строку
    )

    assert block is None


@pytest.mark.asyncio
async def test_all_four_blocks_fit_when_there_is_room(session, family, anya):
    """Сводка собирается из четырёх блоков, и порядок у них закреплённый.

    Просрочка идёт первой не по красоте: бюджет символов бегущий, и обрезаться
    обязан справочный хвост, а не сегодняшние дела.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Старый долг", due_at=_msk(2026, 8, 20, 10, 0),
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Обед с Аней", due_at=_msk(2026, 8, 27, 14, 0),
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Позвонить Виктории", due_at=_msk(2026, 8, 28, 12, 0),
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Разобрать балкон",
    )

    body, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))

    order = [
        body.index(texts.HEADER_OVERDUE),
        body.index("Обед с Аней"),
        body.index(texts.HEADER_NEXT),
        body.index(texts.HEADER_UNDATED),
    ]
    assert order == sorted(order)
    assert len(body) <= texts.MESSAGE_LIMIT - texts.DAY_RESERVE


@pytest.mark.asyncio
async def test_day_text_is_stable_within_the_day(session, family, anya):
    """Панель обязана совпадать сама с собой, иначе «not modified» перестаёт
    отсеивать холостые правки и каждая перерисовка тратит лимит чата.

    Новые блоки подписывают записи относительными метками («завтра, 28 авг»), и
    вопрос был, не начнёт ли панель переписываться каждую минуту. Не начнёт:
    `fmt_due` и `fmt_when` считают от локальной даты, шаг у них — сутки. На
    полуночи текст меняется, но там панель и так перевыпускается по `panel_day`.
    """
    from bot.services import digest

    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Позвонить Виктории", due_at=_msk(2026, 8, 28, 12, 0),
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Разобрать балкон",
    )

    morning, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 9, 0))
    evening, _ = await digest.build_day(session, family, _msk(2026, 8, 27, 22, 0))

    assert texts.HEADER_NEXT in morning and texts.HEADER_UNDATED in morning
    assert morning == evening


# --- Купленный список перестаёт занимать экран --------------------------------


@pytest_asyncio.fixture
async def shopping(session, family, anya):
    """Список с тремя пунктами и уже выпущенной панелью — как в `test_lists`."""
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session, family_id=family.id, author_id=anya.id, list_id=lst.id,
        titles=["Молоко", "Хлеб", "Яйца"],
    )
    await repo.set_list_panel(session, lst, 100)
    return lst


def _numbered(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln[:1].isdigit() and ". " in ln]


def _buttons(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_bought_out_list_collapses_to_a_summary(
    session, family, anya, shopping, bot
):
    """«Почему покупки остаются после того, как всё куплено».

    До правки закрытие меняло только футер и нижнюю кнопку: все строки и все
    чекбоксы оставались висеть в чате навсегда. Список был закрыт логически, а
    на экране не менялось ничего.
    """
    from bot.handlers import lists

    call = _FakeCall(bot, family.chat_id)
    for entry in await repo.list_items(session, shopping.id):
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )

    text, markup = call.edits[-1]
    assert shopping.archived is True
    assert _numbered(text) == []
    assert "куплено 3 из 3" in text
    # Чекбоксов не осталось — случайным тапом список больше не воскресить
    assert _buttons(markup) == [texts.BTN_REOPEN_LIST]


@pytest.mark.asyncio
async def test_closed_list_still_shows_what_was_not_bought(
    session, family, shopping, bot
):
    """Схлопнуть остаток до одной строки нельзя.

    Довод «остаток достанут кнопкой ↩️» держится на том, что кнопка всегда
    сработает, — а `reopen_list` отказывает, когда уже открыт новый список.
    Тогда недокупленное не видно нигде.
    """
    from bot.handlers import lists

    call = _FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )

    text, markup = call.edits[-1]
    assert _numbered(text) == []          # схлопнута
    assert "Молоко" in text and "Яйца" in text  # но остаток на глазах
    assert _buttons(markup) == [texts.BTN_REOPEN_LIST]


@pytest.mark.asyncio
async def test_collapsed_panel_stays_short(session, family, anya):
    """Схлопывание обязано схлопывать: тридцать пунктов — не тридцать строк."""
    from bot.handlers import lists

    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session, family_id=family.id, author_id=anya.id, list_id=lst.id,
        titles=["<" * texts.MAX_ITEM_TITLE] * texts.MAX_LIST_ITEMS,
    )
    items = await repo.list_items(session, lst.id)
    lst.archived = True
    await session.commit()

    text, markup = lists._render(family, lst, items)

    assert len(text) <= texts.MESSAGE_LIMIT
    assert text.count("•") <= texts.MAX_LEFTOVER_ITEMS
    assert texts.MORE_ITEMS.format(count=texts.MAX_LIST_ITEMS - texts.MAX_LEFTOVER_ITEMS) in text
    assert _buttons(markup) == [texts.BTN_REOPEN_LIST]


@pytest.mark.asyncio
async def test_panel_survives_a_list_closed_before_the_migration(
    session, family, shopping
):
    """У списков, закрытых до появления `closed_at`, дата пуста — молчим о ней."""
    from bot.handlers import lists

    shopping.archived = True
    shopping.closed_at = None
    await session.commit()

    text, _ = lists._render(family, shopping, await repo.list_items(session, shopping.id))

    assert "куплено 0 из 3" in text
    assert "Закрыт" not in text


@pytest.mark.asyncio
async def test_old_panel_tap_does_not_revive_a_superseded_list(
    session, family, anya, shopping, bot
):
    """Зомби-список: тап по галке в старой панели оживлял закрытый список.

    Если к тому времени заведён новый, разархивированный старый пропадал
    отовсюду: `active_list` берёт последний по id, то есть новый, а
    `closed_list_with_leftovers` требует `archived IS TRUE` — там его тоже нет.
    Список оставался жив только в уже висящем сообщении.
    """
    from bot.handlers import lists

    call = _FakeCall(bot, family.chat_id)
    items = await repo.list_items(session, shopping.id)
    for entry in items:
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )
    fresh = await repo.get_or_create_active_list(session, family.id)
    assert fresh.id != shopping.id

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, family, anya, bot
    )

    await session.refresh(shopping)
    assert shopping.archived is True
    assert (await repo.active_list(session, family.id)).id == fresh.id
    assert call.answers[-1][0] == texts.LIST_SUPERSEDED
    # Галка при этом снята: пункт остался открытым там, где лежал
    assert (await repo.get_entry(session, items[0].id)).status == "open"


@pytest.mark.asyncio
async def test_untick_still_revives_when_there_is_no_successor(
    session, family, anya, shopping, bot
):
    """Обратная половина: без преемника промах пальцем по-прежнему откатывается.

    Инвариант старый и его нельзя потерять — автоархив без отката делает промах
    в магазине неисправимым.
    """
    from bot.handlers import lists

    call = _FakeCall(bot, family.chat_id)
    items = await repo.list_items(session, shopping.id)
    for entry in items:
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )
    assert shopping.archived is True

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, family, anya, bot
    )

    assert shopping.archived is False
    assert shopping.closed_at is None
    assert (await repo.active_list(session, family.id)).id == shopping.id

