"""Регрессии по ревизии этапов 0–7 (29.08.2026).

Второй файл регрессий, а не дописка в `test_regressions.py`: тот заведён под
ревизию этапов 0–1 и своей шапкой это заявляет. Смешивать поводы — значит через
полгода не понять, какой тест какой баг стережёт.

Правило то же: **каждый тест обязан падать на коде «до правки»**. Если один из
них станет зелёным при откате фикса, он потерял смысл.

Общая черта почти всех найденных ошибок — не архитектура, а расхождение между
соседними путями: одно место соблюдает правило проекта, другое, написанное
раньше или позже, его теряет. Поэтому многие тесты здесь сравнивают два пути
между собой, а не с константой.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text as sql_text

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.handlers import capture, lists, new_entry, review, views
from bot.services import digest, panel, parsing
from bot.services import nlp_fallback as nlp
from bot.services import timeutil as tu

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # четверг, 12:00 по Москве


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


class FakeCall:
    """Колбэк: `answer` + `message.edit_text`, как в `test_review.py`."""

    def __init__(self, chat_id: int = -1001, message_id: int = 500):
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, object]] = []
        self.message = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, type="supergroup"),
            edit_text=self._edit,
        )

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def _edit(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))

    @property
    def alert(self) -> str:
        return self.answers[-1][0] if self.answers else ""


# --- A1: разбор незакрытого падал на записи, у которой сняли срок -------------
#
# Кнопку «напомнить за 15 минут» выпускает успешный перенос, но между её
# выпуском и тапом срок у записи можно снять карточкой (этап 7). `_live_entry`
# смотрел только на семью и статус, и дальше `_remind` считал `None - timedelta`.
# Упавший хендлер стоит апдейта: offset Telegram сдвигается независимо от исхода.


@pytest_asyncio.fixture
async def overdue_entry(session, family, anya):
    return await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="Сдать анализы",
        due_at=_msk(2026, 8, 20, 10, 0),
    )


@pytest.mark.asyncio
async def test_review_survives_an_entry_that_lost_its_due(
    session, family, anya, bot, overdue_entry
):
    """Тап «напомнить» по записи без срока обязан ответить, а не упасть."""
    overdue_entry.due_at = None
    await session.commit()

    call = FakeCall()
    await review.tap(
        call,
        kb.ReviewCB(action="rem", entry_id=overdue_entry.id, value=15),
        session,
        family,
        anya,
        bot,
    )
    assert call.alert == texts.REVIEW_STALE


@pytest.mark.asyncio
async def test_review_survives_norem_on_an_entry_without_due(
    session, family, anya, bot, overdue_entry
):
    """Вторая половина той же дыры: «без напоминания» звало `fmt_due(None)`."""
    overdue_entry.due_at = None
    await session.commit()

    call = FakeCall()
    await review.tap(
        call,
        kb.ReviewCB(action="norem", entry_id=overdue_entry.id),
        session,
        family,
        anya,
        bot,
    )
    assert call.alert == texts.REVIEW_STALE


# --- A2: потолок 4096 считался по числу записей, а не по символам -------------
#
# Инвариант проекта: «Любое сообщение, длину которого задаёт не разработчик,
# обязано иметь потолок». Потолки были — но все по числу элементов, а длину
# строки задаёт человек: заголовок `String(500)` × 20 записей = 10 000 символов.


async def _many_long_entries(session, family, anya, count: int, *, due_at=None):
    for i in range(count):
        await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind="task",
            title=f"{i:02d} " + "молоко " * 70,  # ~500 символов, как в базе
            due_at=due_at,
        )


@pytest.mark.asyncio
async def test_find_stays_under_the_telegram_limit(session, family, anya):
    """20 длинных совпадений не должны превращать выдачу в отказ Telegram."""
    await _many_long_entries(session, family, anya, repo.SEARCH_LIMIT)

    message = _FakeMessage()
    await views.cmd_find(
        message, SimpleNamespace(args="молоко"), session, family
    )
    assert message.texts, "выдача должна быть"
    assert len(message.texts[0]) <= texts.MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_day_digest_stays_under_the_telegram_limit(session, family, anya):
    """MAX_DAY_ITEMS × 500 символов — 7500, то есть отказ и молча потерянная сводка."""
    await _many_long_entries(
        session, family, anya, texts.MAX_DAY_ITEMS, due_at=_msk(2026, 8, 27, 10, 0)
    )

    body, has_content = await digest.build_day(session, family, NOW)
    assert has_content
    # Запас на рамки, в которые сводку заворачивают снаружи: заголовок
    # дайджеста, пометка об опоздании, рамка панели
    assert len(body) <= texts.MESSAGE_LIMIT - texts.DAY_RESERVE


@pytest.mark.asyncio
async def test_week_stays_under_the_telegram_limit(session, family, anya):
    """MAX_WEEK_ITEMS считает записи; 30 длинных заголовков дают 15 000 символов."""
    for day in range(5):
        await _many_long_entries(
            session, family, anya, 7, due_at=_msk(2026, 8, 24 + day, 10, 0)
        )

    message = _FakeMessage()
    await views.cmd_week(message, session, family)
    assert message.texts
    assert len(message.texts[0]) <= texts.MESSAGE_LIMIT


def test_entry_lines_cuts_by_characters_not_only_by_count():
    """Потолков должно быть два. Счёт по записям от лимита Telegram не спасает."""
    entries = [
        SimpleNamespace(
            id=i,
            kind="task",
            title="я" * 500,
            status="open",
            due_at=None,
            all_day=False,
            author=None,
            closer=None,
            assignee=None,
            done_at=None,
            created_at=NOW,
            source_chat_id=None,
            source_message_id=None,
        )
        for i in range(15)
    ]
    lines = texts.entry_lines(entries, MSK, NOW, limit=15, budget=2000)
    assert len("\n".join(lines)) <= 2000
    assert len(lines) < 15, "часть строк обязана отвалиться"
    assert "ещё" in lines[-1], "и про отброшенные надо сказать вслух"


# --- A3: мастер `/new` не резал заголовок -------------------------------------
#
# Три остальных пути создания режут по `parsing.TITLE_LIMIT`. `Entry.title`
# объявлен `String(500)`, но SQLite длину VARCHAR не проверяет — вставленный
# абзац сохранялся целиком и ломал потом `/today`, панель и сводку разом.


@pytest.mark.asyncio
async def test_wizard_cuts_the_title_like_every_other_path():
    state = _FakeState()
    await new_entry.take_title(_FakeMessage("э" * 5000), state)
    assert len(state.data["title"]) == parsing.TITLE_LIMIT


# --- A4: `/buy` перерисовывал панель списка мимо лока --------------------------
#
# Лок брали `tick`, `close_list`, `reopen` и `refresh_panel`, а `_open` — нет,
# хотя `_show` делает ровно «прочитать → отрисовать → отредактировать».


@pytest.mark.asyncio
async def test_buy_redraws_the_list_panel_under_the_lock(
    session, family, anya, bot, monkeypatch
):
    held: list[bool] = []
    original = lists._show

    async def spy(bot_, session_, family_, lst, message, **kwargs):
        lock = lists._locks.get(lst.id)
        held.append(lock is not None and lock.locked())
        return await original(bot_, session_, family_, lst, message, **kwargs)

    monkeypatch.setattr(lists, "_show", spy)

    message = _FakeMessage("/buy молоко")
    await lists._open(message, "молоко", session, family, anya, bot)
    assert held == [True], "перерисовка обязана идти под локом списка"

    # И второй вход — показ уже существующего списка без новых пунктов
    held.clear()
    await lists._open(message, "", session, family, anya, bot)
    assert held == [True]


# --- A5: дебаунс панели вычёркивал из словаря чужую задачу --------------------
#
# Отмена доставляется не в момент `cancel()`, а на ближайшем `await`. Задача,
# досыпавшая `sleep`, но не получившая управление, успевала снять с ключа
# задачу-преемницу — вместе с сильной ссылкой и возможностью её отменить.


@pytest.mark.asyncio
async def test_debounce_never_drops_a_newer_task(monkeypatch, bot):
    """Проверяем инвариант, а не гонку: «снимает с ключа только себя».

    Гонку саму по себе воспроизвести устойчиво нельзя — она зависит от того, в
    какой именно момент цикл событий доставит отмену. Зато её следствие
    формулируется точно и проверяется в один шаг: под ключом лежит **чужая**
    задача, и `_debounced` не имеет права её вычёркивать. Безусловный
    `_tasks.pop(family_id)` этот тест валит.
    """
    monkeypatch.setattr(panel.settings, "panel_debounce_seconds", 0)

    refreshed: list[int] = []

    async def fake_refresh(bot_, family_id, last_message_id=None):
        refreshed.append(family_id)

    monkeypatch.setattr(panel, "refresh", fake_refresh)

    async def idle():
        await asyncio.sleep(3600)

    successor = asyncio.create_task(idle())
    panel._tasks[1] = successor
    try:
        await asyncio.create_task(panel._debounced(bot, 1, None))
        assert panel._tasks.get(1) is successor, (
            "задача вычеркнула из словаря преемницу: с ней уходит и сильная "
            "ссылка, ради которой словарь заведён, и возможность отменить её "
            "следующим schedule, то есть сам дебаунс"
        )
        assert refreshed == [1], "свою работу она при этом обязана доделать"
    finally:
        successor.cancel()


# --- A6: правка даты на карточке показывала выдуманное время ------------------
#
# `dateparser` берёт неназванное время из «сейчас»: «завтра» в 14:37 → «завтра
# в 14:37». `_fallback` это ловил, а `edit_field`, написанный позже, — нет.


def test_bare_day_is_all_day_wherever_dateparser_is_used():
    now_local = datetime(2026, 8, 27, 14, 37)
    parsed = nlp.parse_when("завтра", now_local)
    assert parsed is not None

    due_at, all_day = nlp.as_due(parsed.when, now_local)
    assert all_day, "время никто не называл — значит весь день"
    assert (due_at.hour, due_at.minute) == (0, 0)


def test_named_time_survives_as_due():
    """Обратная половина: названное время выдумкой считать нельзя."""
    now_local = datetime(2026, 8, 27, 14, 37)
    parsed = nlp.parse_when("завтра в 19:00", now_local)
    assert parsed is not None

    due_at, all_day = nlp.as_due(parsed.when, now_local)
    assert not all_day
    assert (due_at.hour, due_at.minute) == (19, 0)


# --- A7: границы года проверялись только на пути модели -----------------------
#
# `parsing._dt` режет по MIN_YEAR/MAX_YEAR, а `nlp_fallback` — нет, хотя его
# результат уходит в `to_utc` из четырёх мест. `OverflowError` там — падение
# хендлера ещё до карточки, то есть снова потерянный апдейт.


# Именно эти три `dateparser` отдаёт как есть — проверено на живой библиотеке.
# Год 1 сюда не входит: его он молча заменяет на текущий, то есть тест на нём
# был бы зелёным и до правки
@pytest.mark.parametrize("year", [1899, 2101, 9999])
def test_dateparser_dates_outside_the_sane_range_are_refused(year):
    now_local = datetime(2026, 8, 27, 12, 0)
    parsed = nlp.parse_when(f"1 сентября {year} позвонить маме", now_local)
    assert parsed is None or parsing.MIN_YEAR <= parsed.when.year <= parsing.MAX_YEAR


def test_parse_when_bounds_match_the_llm_path():
    """Границы обязаны быть одни на оба пути, а не две независимые копии."""
    assert (nlp.MIN_YEAR, nlp.MAX_YEAR) == (parsing.MIN_YEAR, parsing.MAX_YEAR)


# --- A9: внешние ключи SQLite были выключены ----------------------------------


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(session):
    """Без `PRAGMA foreign_keys=ON` все FK в схеме — декларация, а не защита."""
    value = await session.execute(sql_text("PRAGMA foreign_keys"))
    assert value.scalar() == 1


# --- A11: карточка разбора молчала на неизвестной кнопке ----------------------


@pytest.mark.asyncio
async def test_capture_card_always_answers_the_tap(session, family, anya, bot):
    """Без ответа на колбэк у нажавшего крутится индикатор до таймаута."""
    key = (-1001, 500)
    capture._drafts[key] = capture.Draft(
        family_id=family.id,
        items=[parsing.Item(kind="task", title="Хлеб")],
        source_message_id=1,
    )
    try:
        call = FakeCall(message_id=500)
        await capture.tap(
            call,
            kb.CaptureCB(action="нет-такого-действия"),
            session,
            family,
            anya,
            bot,
        )
        assert call.answers, "на колбэк обязан быть ответ"
    finally:
        capture._drafts.pop(key, None)


# --- Заглушки ------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, text: str = "", chat_id: int = -1001):
        self.text = text
        self.message_id = 900
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.replies: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=901)

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.replies]


class _FakeState:
    def __init__(self):
        self.data: dict = {}
        self.state = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state
