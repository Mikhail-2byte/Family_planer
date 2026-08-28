"""Списки покупок — этап 4.

Панель списка живёт в чате как одно сообщение с чекбоксами, поэтому почти
каждый тест проверяет не «что в базе», а «сколько сообщений отправлено и
сколько отредактировано»: критерий закрытия этапа сформулирован именно так.
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot import texts
from bot.db import repo
from bot import keyboards as kb
from bot.handlers import capture, lists
from bot.services import parsing


@pytest.fixture(autouse=True)
def _list_locks():
    """Локи ключуются `list_id`, а он в тестах на общей БД в памяти повторяется."""
    lists._locks.clear()
    yield
    lists._locks.clear()


class FakeCall:
    """Колбэк: `answer` + `message.edit_text`.

    `FakeMessage` из `conftest.py` не подходит — у него нет `edit_text`, а
    именно им панель и правится.
    """

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


class PanelMessage:
    """Сообщение-команда: `answer` возвращает объект с последовательным id."""

    def __init__(self, message_id: int = 200):
        self.message_id = message_id
        self.sent: list[tuple[str, object]] = []
        self._next = message_id

    async def answer(self, text: str, reply_markup=None):
        self.sent.append((text, reply_markup))
        self._next += 1
        return SimpleNamespace(message_id=self._next)

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.sent]


@pytest_asyncio.fixture
async def shopping(session, family, anya):
    """Список с тремя пунктами и уже выпущенной панелью."""
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session,
        family_id=family.id,
        author_id=anya.id,
        list_id=lst.id,
        titles=["Молоко", "Хлеб", "Яйца"],
    )
    await repo.set_list_panel(session, lst, 100)
    return lst


def _buttons(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def _numbered(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln[:1].isdigit() and ". " in ln]


# --- 4.1 Списки ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_creates_list_and_adds_items(session, family, anya, bot):
    message = PanelMessage()
    await lists._open(message, "молоко, хлеб", session, family, anya, bot)

    lst = await repo.active_list(session, family.id)
    assert lst is not None and lst.kind == "shopping"
    items = await repo.list_items(session, lst.id)
    assert [e.title for e in items] == ["молоко", "хлеб"]
    assert all(e.kind == "shopping" and e.list_id == lst.id for e in items)
    assert [e.position for e in items] == [1, 2]


@pytest.mark.asyncio
async def test_items_keep_their_order(session, family, anya, bot):
    """Критерий 4.1: «порядок сохраняется» — в том числе между заходами."""
    message = PanelMessage()
    await lists._open(message, "молоко", session, family, anya, bot)
    await lists._open(message, "хлеб\nяйца", session, family, anya, bot)

    lst = await repo.active_list(session, family.id)
    items = await repo.list_items(session, lst.id)
    assert [e.title for e in items] == ["молоко", "хлеб", "яйца"]
    assert [e.position for e in items] == [1, 2, 3]


@pytest.mark.asyncio
async def test_bare_buy_creates_nothing(session, family, anya, bot):
    """Пустой строки в `lists` быть не должно.

    `repo._family_is_empty` считает списки, и семья со строкой в `lists`
    перестаёт быть пустышкой — а на этой проверке держится переезд в
    супергруппу, где второго шанса не будет.
    """
    message = PanelMessage()
    await lists._open(message, "", session, family, anya, bot)

    assert message.texts == [texts.BUY_USAGE]
    assert await repo.active_list(session, family.id) is None


@pytest.mark.asyncio
async def test_long_title_is_cut_on_input(session, family, anya, bot):
    """Пункт, не влезающий в панель, остался бы без кнопки — и список
    никогда не стал бы закрытым."""
    message = PanelMessage()
    await lists._open(message, "я" * 500, session, family, anya, bot)

    lst = await repo.active_list(session, family.id)
    items = await repo.list_items(session, lst.id)
    assert len(items[0].title) == texts.MAX_ITEM_TITLE


@pytest.mark.asyncio
async def test_buy_refuses_to_grow_past_the_cap(session, family, anya, bot):
    message = PanelMessage()
    await lists._open(
        message, ",".join(f"п{i}" for i in range(texts.MAX_LIST_ITEMS)), session,
        family, anya, bot,
    )
    await lists._open(message, "лишнее", session, family, anya, bot)

    lst = await repo.active_list(session, family.id)
    items = await repo.list_items(session, lst.id)
    assert len(items) == texts.MAX_LIST_ITEMS
    assert texts.list_full() in message.texts


@pytest.mark.asyncio
async def test_items_that_did_not_fit_are_named_out_loud(session, family, anya, bot):
    """Ревизия нашла: лишние пункты отбрасывались молча.

    Набрал пять, увидел два — без объяснения это читается как «бот проглотил»,
    и человек набирает их заново.
    """
    message = PanelMessage()
    await lists._open(
        message,
        ",".join(f"п{i}" for i in range(texts.MAX_LIST_ITEMS - 2)),
        session,
        family,
        anya,
        bot,
    )
    await lists._open(message, "a,b,c,d,e", session, family, anya, bot)

    lst = await repo.active_list(session, family.id)
    assert len(await repo.list_items(session, lst.id)) == texts.MAX_LIST_ITEMS
    assert texts.list_partial(2, 3) in message.texts


# --- 4.2 Панель ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_buy_edits_the_panel_in_place(session, family, shopping, anya, bot):
    """Критерий закрытия этапа: панель правится, а не дублируется."""
    message = PanelMessage(message_id=105)
    await lists._open(message, "сыр", session, family, anya, bot)

    assert message.sent == []  # нового сообщения нет
    assert len(bot.edited) == 1
    chat_id, message_id, text = bot.edited[0]
    assert (chat_id, message_id) == (family.chat_id, 100)
    assert "сыр" in text


@pytest.mark.asyncio
async def test_edited_panel_keeps_its_buttons(session, family, shopping, anya, bot):
    """`editMessageText` без `reply_markup` снял бы клавиатуру, и список
    превратился бы в текст без единого чекбокса."""
    message = PanelMessage(message_id=105)
    await lists._open(message, "сыр", session, family, anya, bot)

    markup = bot.edit_kwargs[0]["reply_markup"]
    assert len(_buttons(markup)) == 5  # четыре пункта + «Список закрыт»


@pytest.mark.asyncio
async def test_panel_republished_when_scrolled_away(
    session, family, shopping, anya, bot, monkeypatch
):
    monkeypatch.setattr(lists.settings, "panel_max_messages", 5)
    message = PanelMessage(message_id=200)  # 200 - 100 = 100 > 5

    await lists._open(message, "сыр", session, family, anya, bot)

    assert bot.edited == []
    assert len(message.sent) == 1
    assert shopping.panel_message_id == 201  # id перепривязан


@pytest.mark.asyncio
async def test_bare_buy_always_publishes_a_fresh_panel(
    session, family, shopping, anya, bot
):
    """Человек попросил показать список — он должен его увидеть, а не получить
    правку сообщения, уехавшего вверх."""
    message = PanelMessage(message_id=101)
    await lists._open(message, "", session, family, anya, bot)

    assert len(message.sent) == 1
    assert bot.edited == []


@pytest.mark.asyncio
async def test_panel_survives_restart(session, family, shopping, anya, bot):
    """Смысл колонки `lists.panel_message_id`: id живёт в базе, а не в памяти."""
    reloaded = await repo.active_list(session, family.id)
    assert reloaded.panel_message_id == 100

    message = PanelMessage(message_id=102)
    await lists._open(message, "вода", session, family, anya, bot)
    assert bot.edited and bot.edited[0][1] == 100


@pytest.mark.asyncio
async def test_tap_strikes_through_by_editing(session, family, shopping, anya, bot):
    """Критерий закрытия этапа: тап зачёркивает правкой, а не новым сообщением."""
    items = await repo.list_items(session, shopping.id)
    call = FakeCall(bot, family.chat_id)

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[1].id), session, family, anya, bot
    )

    assert len(call.edits) == 1
    text, markup = call.edits[0]
    assert "<s>Хлеб</s>" in text
    assert _buttons(markup)[1] == "✅ 2"
    assert bot.sent == []


@pytest.mark.asyncio
async def test_tap_again_unticks(session, family, shopping, anya, bot):
    items = await repo.list_items(session, shopping.id)
    call = FakeCall(bot, family.chat_id)
    cb = lists.ListCB(action="tick", target=items[0].id)

    await lists.tick(call, cb, session, family, anya, bot)
    await lists.tick(call, cb, session, family, anya, bot)

    entry = await repo.get_entry(session, items[0].id)
    assert entry.status == "open"
    assert entry.done_at is None and entry.done_by is None


@pytest.mark.asyncio
async def test_buttons_match_numbered_lines(session, family, shopping, anya, bot):
    """Рассинхрон номеров молча уводит тап не на тот пункт."""
    items = await repo.list_items(session, shopping.id)
    text, markup = lists._render(family, shopping, items)
    assert len(_numbered(text)) == len(_buttons(markup)) - 1  # минус «Список закрыт»


@pytest.mark.asyncio
async def test_panel_stays_under_the_telegram_limit(session, family, anya):
    """Тридцать пунктов с угловыми скобками: `_escape` раздувает «<» вчетверо."""
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session,
        family_id=family.id,
        author_id=anya.id,
        list_id=lst.id,
        titles=["<" * texts.MAX_ITEM_TITLE] * texts.MAX_LIST_ITEMS,
    )
    items = await repo.list_items(session, lst.id)

    text, markup = lists._render(family, lst, items)
    assert len(text) <= texts.MESSAGE_LIMIT
    assert len(_numbered(text)) == len(_buttons(markup)) - 1
    assert len(_buttons(markup)) <= 100  # потолок Telegram


@pytest.mark.asyncio
async def test_concurrent_taps_do_not_lose_each_other(
    session_maker, session, family, anya, shopping, bot, monkeypatch
):
    """Критерий закрытия этапа: двое тапают одновременно.

    Каждому тапу — своя сессия: в бою их открывает `FamilyMiddleware`, а одна
    `AsyncSession` на две задачи не рассчитана в принципе.

    Проверяется **прямое свойство лока: критические секции не переплетаются**, а
    не последствие гонки на живых таймингах. Через тайминги её поймать не
    вышло — обе транзакции успевают закоммититься раньше любого чтения, так что
    устаревшего рендера просто не возникает, и тест зеленел бы и без лока.
    Ловить редкое окно подкрученными задержками значит писать мигающий тест, а
    такой в проекте уже вычищали (см. этап 2п).

    Смысл свойства: без лока «прочитать → отрисовать → отредактировать» одного
    тапа может разорваться посередине чужим тапом, и тогда последней в чат
    приезжает правка, собранная по устаревшему чтению, — панель застревает без
    чужого пункта до следующего тапа. Это и есть «потеря» из критерия этапа.
    """
    items = await repo.list_items(session, shopping.id)
    first, third = items[0].id, items[2].id

    # Журнал входов и выходов из критической секции
    trace: list[str] = []
    edits: list[str] = []
    original_edit = lists.edit_or_ignore

    async def traced_edit(call, text, markup):
        trace.append("вошли")
        await asyncio.sleep(0.05)  # окно, в которое чужой тап мог бы вклиниться
        await original_edit(call, text, markup)
        edits.append(text)
        trace.append("вышли")

    monkeypatch.setattr(lists, "edit_or_ignore", traced_edit)

    async def tap(entry_id: int):
        call = FakeCall(bot, family.chat_id)
        async with session_maker() as own:
            fam = await repo.get_family_by_id(own, family.id)
            member = await repo.get_or_create_member(own, family.id, 222, "Аня")
            await lists.tick(
                call,
                lists.ListCB(action="tick", target=entry_id),
                own,
                fam,
                member,
                bot,
            )

    await asyncio.gather(tap(first), tap(third))

    # Без лока было бы ['вошли', 'вошли', 'вышли', 'вышли']
    assert trace == ["вошли", "вышли", "вошли", "вышли"]

    refreshed = await repo.list_items(session, shopping.id)
    for entry in refreshed:
        await session.refresh(entry)
    assert [e.status for e in refreshed] == ["done", "open", "done"]

    # Последняя доехавшая правка показывает оба вычеркнутыми
    assert edits[-1].count("<s>") == 2


@pytest.mark.asyncio
async def test_alien_family_cannot_toggle(session, family, shopping, anya, bot):
    """Изоляция по `family_id` — зеркало `complete_entry`."""
    items = await repo.list_items(session, shopping.id)
    other = await repo.get_or_create_family(session, -1002, "Чужие")
    call = FakeCall(bot, other.chat_id)

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, other, anya, bot
    )

    assert call.answers == [(texts.LIST_ITEM_GONE, True)]
    assert call.edits == []
    assert (await repo.get_entry(session, items[0].id)).status == "open"


# --- 4.3 Авторство и архив ----------------------------------------------------


@pytest.mark.asyncio
async def test_closer_is_shown_not_author(session, family, shopping, bot):
    """4.3: в строке видно, кто купил, а не кто добавил."""
    misha = await repo.get_or_create_member(session, family.id, 111, "Миша")
    items = await repo.list_items(session, shopping.id)  # автор всех — Аня
    call = FakeCall(bot, family.chat_id)

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, family, misha, bot
    )

    line = _numbered(call.edits[0][0])[0]
    assert "Миша" in line and "Аня" not in line


@pytest.mark.asyncio
async def test_list_archives_when_everything_is_bought(
    session, family, shopping, anya, bot
):
    items = await repo.list_items(session, shopping.id)
    call = FakeCall(bot, family.chat_id)
    for entry in items:
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )

    assert shopping.archived is True
    assert await repo.active_list(session, family.id) is None
    assert texts.LIST_ALL_DONE in call.edits[-1][0]


@pytest.mark.asyncio
async def test_untick_revives_the_archived_list(session, family, shopping, anya, bot):
    """Промах пальцем в магазине обязан отменяться тем же движением."""
    items = await repo.list_items(session, shopping.id)
    call = FakeCall(bot, family.chat_id)
    for entry in items:
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )
    assert await repo.active_list(session, family.id) is None

    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, family, anya, bot
    )

    assert shopping.archived is False
    assert (await repo.active_list(session, family.id)).id == shopping.id


@pytest.mark.asyncio
async def test_empty_list_is_not_archived(session, family):
    """Иначе только что созданный список сразу уезжал бы в архив."""
    lst = await repo.get_or_create_active_list(session, family.id)
    assert await repo.sync_list_archived(session, lst) is False
    assert (await repo.active_list(session, family.id)).id == lst.id


@pytest.mark.asyncio
async def test_close_button_archives_a_half_bought_list(
    session, family, shopping, bot
):
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )

    assert shopping.archived is True
    assert await repo.active_list(session, family.id) is None


@pytest.mark.asyncio
async def test_explicit_close_survives_the_next_tap(
    session, family, shopping, anya, bot
):
    """Ревизия нашла: симметричный пересчёт `archived` стирал явное закрытие.

    Список, закрытый кнопкой с недокупленным остатком, оживал от первого же
    тапа по этому остатку — то есть кнопка не значила ничего.
    """
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )
    items = await repo.list_items(session, shopping.id)

    # Докупили ещё один пункт — закрытие это не отменяет
    await lists.tick(
        call, lists.ListCB(action="tick", target=items[0].id), session, family, anya, bot
    )

    assert shopping.archived is True
    assert await repo.active_list(session, family.id) is None


@pytest.mark.asyncio
async def test_closed_list_panel_says_so(session, family, shopping, bot):
    """Иначе панель закрытого списка неотличима от открытой: тот же футер
    «Тапните номер, чтобы вычеркнуть» и никакого следа закрытия."""
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )

    assert texts.LIST_CLOSED_FOOTER in call.edits[-1][0]
    assert texts.LIST_HINT not in call.edits[-1][0]


@pytest.mark.asyncio
async def test_panel_length_is_measured_with_the_real_footer(session, family, anya):
    """Мерили самым коротким футером, а подставить могли длинный — панель
    перерастала лимит на разницу между ними."""
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session,
        family_id=family.id,
        author_id=anya.id,
        list_id=lst.id,
        titles=["<" * texts.MAX_ITEM_TITLE] * texts.MAX_LIST_ITEMS,
    )
    items = await repo.list_items(session, lst.id)
    for entry in items:
        entry.status, entry.done_by = "done", anya.id
    await session.commit()

    for closed in (False, True):
        lst.archived = closed
        text, markup = lists._render(family, lst, items)
        assert len(text) <= texts.MESSAGE_LIMIT
        assert len(_numbered(text)) == len(_buttons(markup)) - 1


@pytest.mark.asyncio
async def test_buy_after_archive_starts_a_new_list(
    session, family, shopping, anya, bot
):
    shopping.archived = True
    await session.commit()

    message = PanelMessage(message_id=300)
    await lists._open(message, "кефир", session, family, anya, bot)

    fresh = await repo.active_list(session, family.id)
    assert fresh is not None and fresh.id != shopping.id
    assert len(await repo.list_items(session, shopping.id)) == 3  # старый цел


# --- 4.6 Утечки ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_items_do_not_leak_into_other_views(session, family, shopping):
    """У пунктов нет `due_at`, и на этом держится их отсутствие в дне.

    Первый же вызов `entries_by_kind` с `kind='shopping'` эту границу откроет —
    поэтому проверяем именно её, а не только текущее поведение.
    """
    from datetime import datetime

    from bot.services import digest

    body, _ = await digest.build_day(session, family, datetime(2026, 8, 28, 9, 0))
    assert "Молоко" not in body

    for kind in ("task", "note", "event"):
        _, total = await repo.entries_by_kind(session, family.id, kind)
        assert total == 0

    assert not await repo.overdue_entries(session, family.id)


@pytest.mark.asyncio
async def test_day_shows_a_shopping_counter_not_the_items(session, family, shopping):
    """4.4: в день попадает счётчик, а не тридцать строк списка."""
    from datetime import datetime

    from bot.services import digest

    body, _ = await digest.build_day(session, family, datetime(2026, 8, 28, 9, 0))
    assert texts.shopping_summary(shopping.name, 3) in body


@pytest.mark.asyncio
async def test_shopping_does_not_wake_the_morning_digest(session, family, shopping):
    """Иначе утренняя сводка уходила бы каждый день, пока в списке хоть что-то
    лежит, и «день пуст» перестало бы означать «сегодня ничего нет»."""
    from datetime import datetime

    from bot.services import digest

    _, has_content = await digest.build_day(
        session, family, datetime(2026, 8, 28, 9, 0)
    )
    assert has_content is False


@pytest.mark.asyncio
async def test_bought_out_list_leaves_the_day(session, family, shopping, anya, bot):
    """Закрытый список из дня уходит — счётчик считает только открытые."""
    from datetime import datetime

    from bot.services import digest

    call = FakeCall(bot, family.chat_id)
    for entry in await repo.list_items(session, shopping.id):
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )

    body, _ = await digest.build_day(session, family, datetime(2026, 8, 28, 9, 0))
    assert "/buy" not in body


@pytest.mark.asyncio
async def test_closed_list_with_leftovers_is_still_reachable(
    session, family, shopping, anya, bot
):
    """Ревизия внесла дефект: липкое закрытие сделало остаток недостижимым.

    Из `/buy` список ушёл, а тап по старой панели его не оживляет — оживляет
    только снятая галка, а у непокупленных пунктов её нет.
    """
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )
    assert await repo.active_list(session, family.id) is None

    message = PanelMessage(message_id=400)
    await lists._open(message, "", session, family, anya, bot)

    text, markup = message.sent[0]
    assert "Молоко" in text
    assert texts.BTN_REOPEN_LIST in _buttons(markup)
    assert texts.BTN_CLOSE_LIST not in _buttons(markup)


@pytest.mark.asyncio
async def test_reopen_returns_the_list_to_work(session, family, shopping, bot):
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )

    await lists.reopen(
        call, lists.ListCB(action="reopen", target=shopping.id), session, family, bot
    )

    assert shopping.archived is False
    assert (await repo.active_list(session, family.id)).id == shopping.id
    assert texts.BTN_CLOSE_LIST in _buttons(call.edits[-1][1])


@pytest.mark.asyncio
async def test_bought_out_closed_list_does_not_come_back(
    session, family, shopping, anya, bot
):
    """Возвращается только список с непокупленным остатком.

    Полностью купленный закрыт по делу, и `/buy` обязан начать новый.
    """
    call = FakeCall(bot, family.chat_id)
    for entry in await repo.list_items(session, shopping.id):
        await lists.tick(
            call, lists.ListCB(action="tick", target=entry.id), session, family, anya, bot
        )
    assert shopping.archived is True

    message = PanelMessage(message_id=400)
    await lists._open(message, "", session, family, anya, bot)
    assert message.texts == [texts.BUY_USAGE]


# --- Покупки из мастера и из разбора попадают в список ------------------------


@pytest.mark.asyncio
async def test_wizard_shopping_lands_in_the_list(session, family, anya):
    """Раньше запись проваливалась: без `list_id` и без даты её не показывал
    ни `/buy`, ни день, ни `/tasks` — только `/find`."""
    list_id, position = await repo.shopping_slot(session, family.id)
    entry = await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="shopping",
        title="виски",
        list_id=list_id,
        position=position,
    )

    lst = await repo.active_list(session, family.id)
    assert entry.list_id == lst.id and entry.position == 1
    assert [e.title for e in await repo.list_items(session, lst.id)] == ["виски"]


@pytest.mark.asyncio
async def test_shopping_slot_appends_to_the_existing_list(
    session, family, shopping, anya
):
    list_id, position = await repo.shopping_slot(session, family.id)
    assert list_id == shopping.id
    assert position == 4  # после трёх пунктов фикстуры


@pytest.mark.asyncio
async def test_shopping_slot_starts_a_new_list_instead_of_reviving(
    session, family, shopping, bot
):
    """Новая покупка начинает новый список, а закрытый остаётся закрытым."""
    call = FakeCall(bot, family.chat_id)
    await lists.close_list(
        call, lists.ListCB(action="close", target=shopping.id), session, family, bot
    )

    list_id, position = await repo.shopping_slot(session, family.id)

    assert list_id != shopping.id and position == 1
    assert shopping.archived is True


@pytest.mark.asyncio
async def test_chat_migration_drops_the_list_panel(session, family, shopping):
    """message_id панели принадлежал старому чату — в новом он чужой."""
    await repo.migrate_family_chat_id(session, family.chat_id, -100777)

    await session.refresh(shopping)
    assert shopping.panel_message_id is None


@pytest.mark.asyncio
async def test_capture_save_refreshes_the_list_panel(
    session, family, anya, shopping, bot
):
    """Покупка из разбора «+» обязана оживить панель, а не только базу (4.5).

    До правки `capture` будил только панель дня: пункт в списке появлялся, а
    панель покупок в чате показывала прежние три позиции до ближайшего тапа
    или `/buy`. Расходились не данные, а картинка — но живая панель ради
    картинки и заведена.

    Карточка взята рядом с панелью (105 против 100): так проверяется правка на
    месте. Далеко уехавшая панель — это ветка перевыпуска, у неё свои тесты.
    """
    key = (family.chat_id, 105)
    capture._drafts[key] = capture.Draft(
        family.id,
        [parsing.Item(kind="shopping", title="Сыр", confidence=0.9)],
        source_message_id=104,
    )
    call = FakeCall(bot, family.chat_id, message_id=105)

    await capture.tap(
        call, kb.CaptureCB(action="save"), session, family, anya, bot
    )

    assert bot.edited, "панель списка не тронута"
    chat_id, message_id, text = bot.edited[-1]
    assert (chat_id, message_id) == (family.chat_id, shopping.panel_message_id)
    assert "Сыр" in text
    # Пункт при этом действительно в списке, а не рядом с ним
    assert "Сыр" in [e.title for e in await repo.list_items(session, shopping.id)]


@pytest.mark.asyncio
async def test_capture_save_without_shopping_leaves_the_list_alone(
    session, family, anya, shopping, bot
):
    """Задача покупок не касается — лишняя правка тратила бы лимит чата."""
    key = (family.chat_id, 105)
    capture._drafts[key] = capture.Draft(
        family.id,
        [parsing.Item(kind="task", title="Позвонить маме", confidence=0.9)],
        source_message_id=104,
    )
    call = FakeCall(bot, family.chat_id, message_id=105)

    await capture.tap(
        call, kb.CaptureCB(action="save"), session, family, anya, bot
    )

    assert bot.edited == []
