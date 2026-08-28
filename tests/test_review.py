"""Разбор незакрытого: закрыть или перенести (этап 5п).

Колбэки зовутся напрямую, как в `test_capture.py`; сети нет — `FakeBot` из
`conftest.py` и локальный `FakeCall` с `edit_text`, как в `test_lists.py`.
"""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from bot import keyboards as kb
from bot import texts
from bot.db import repo
from bot.db.models import Reminder
from bot.handlers import review as handler
from bot.services import digest, review, sending
from bot.services import timeutil as tu
from sqlalchemy import select

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # 12:00 по Москве, четверг


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


class FakeCall:
    """Колбэк: `answer` + `message.edit_text`."""

    def __init__(self, chat_id: int, message_id: int = 500):
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
    def screen(self) -> str:
        return self.edits[-1][0] if self.edits else ""

    @property
    def markup(self):
        return self.edits[-1][1] if self.edits else None

    @property
    def alert(self) -> str:
        return self.answers[-1][0] if self.answers else ""


class FakeReply:
    """Ответ реплаем на сообщение разбора."""

    def __init__(self, chat_id: int, text: str, card_id: int = 500):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = 900
        self.reply_to_message = SimpleNamespace(message_id=card_id)
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs) -> None:
        self.replies.append(text)


@pytest.fixture(autouse=True)
def _pending():
    handler._pending.clear()
    yield
    handler._pending.clear()


@pytest.fixture
def overdue(session, family, anya):
    """Заводит просроченную запись и отдаёт её."""

    async def add(title="Позвонить маме", *, kind="task", due=None, all_day=False):
        return await repo.create_entry(
            session,
            family_id=family.id,
            author_id=anya.id,
            kind=kind,
            title=title,
            due_at=due or _msk(2026, 8, 26, 19, 0),
            all_day=all_day,
        )

    return add


# --- сборка сообщения ---------------------------------------------------------


@pytest.mark.asyncio
async def test_buttons_match_numbered_lines(session, family, overdue):
    """Рассинхрон номеров молча уводит «Готово» не на ту запись."""
    for i in range(3):
        await overdue(f"дело {i}")

    entries = await review.overdue(session, family, NOW)
    text, shown = review.render(entries, family.tz, NOW)
    rows = kb.review_keyboard(shown).inline_keyboard

    assert len(shown) == 3
    assert len(rows) == 3
    for i in range(1, 4):
        assert f"\n{i}. " in "\n" + text
        assert rows[i - 1][0].text == f"✅ {i}"


@pytest.mark.asyncio
async def test_extra_entries_are_cut_and_counted(session, family, overdue):
    for i in range(texts.MAX_REVIEW_ITEMS + 3):
        await overdue(f"дело {i}")

    entries = await review.overdue(session, family, NOW)
    text, shown = review.render(entries, family.tz, NOW)

    assert len(shown) == texts.MAX_REVIEW_ITEMS
    assert texts.review_tail(3) in text


@pytest.mark.asyncio
async def test_long_titles_do_not_break_the_limit(session, family, overdue):
    """Потолок по числу записей длину не ограничивает: title — 500 символов."""
    for i in range(texts.MAX_REVIEW_ITEMS):
        await overdue("я" * 500)

    entries = await review.overdue(session, family, NOW)
    text, shown = review.render(entries, family.tz, NOW)

    assert len(text) < texts.MESSAGE_LIMIT
    assert len(shown) == len(kb.review_keyboard(shown).inline_keyboard)


@pytest.mark.asyncio
async def test_nothing_overdue_is_a_clear_message(session, family):
    entries = await review.overdue(session, family, NOW)
    text, shown = review.render(entries, family.tz, NOW)

    assert text == texts.REVIEW_ALL_CLEAR
    assert shown == []
    assert kb.review_keyboard(shown) is None


# --- встраивание в утреннюю сводку -------------------------------------------


@pytest.mark.asyncio
async def test_digest_adds_the_review_message(session, family, anya, bot, overdue):
    await overdue()
    await digest._send_one(bot, session, family, NOW)

    assert len(bot.sent) == 2  # сводка и разбор
    assert texts.REVIEW_HEADER in bot.texts[1]
    assert bot.kwargs[1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_digest_stays_silent_without_overdue(session, family, anya, bot):
    """Сторож против шума: «разбирать нечего» каждое утро читать не станут."""
    await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title="сегодня",
        due_at=_msk(2026, 8, 27, 20, 0),
    )
    await digest._send_one(bot, session, family, NOW)

    assert len(bot.sent) == 1
    assert texts.REVIEW_HEADER not in bot.texts[0]


@pytest.mark.asyncio
async def test_failed_review_does_not_mark_the_day(
    session, family, bot, overdue, monkeypatch
):
    """`RETRY` на разборе — день не отработан, иначе он пропадёт навсегда.

    Та же логика, что у самой сводки: сеть и флуд-контроль это временный сбой,
    и следующий тик обязан попробовать снова.
    """
    await overdue()

    async def flaky(bot_, family_, text, **kwargs):
        # Сводка уходит, разбор упирается в флуд-контроль
        return sending.RETRY if texts.REVIEW_HEADER in text else sending.OK

    monkeypatch.setattr(digest.sending, "deliver", flaky)

    await digest._send_one(bot, session, family, NOW)

    assert family.last_digest_on is None


# --- закрытие -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_closes_and_drops_from_the_list(
    session, family, anya, bot, overdue
):
    entry = await overdue()
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="done", entry_id=entry.id), session, family, anya, bot
    )

    await session.refresh(entry)
    assert entry.status == "done"
    assert entry.done_by == anya.id and entry.done_at is not None
    assert call.screen == texts.REVIEW_ALL_CLEAR


@pytest.mark.asyncio
async def test_done_twice_says_so(session, family, anya, bot, overdue):
    entry = await overdue()
    call = FakeCall(family.chat_id)
    cb = kb.ReviewCB(action="done", entry_id=entry.id)

    await handler.tap(call, cb, session, family, anya, bot)
    await handler.tap(call, cb, session, family, anya, bot)

    assert call.alert == texts.REVIEW_STALE


# --- перенос ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_keeps_the_time_of_day(session, family, anya, bot, overdue):
    entry = await overdue(due=_msk(2026, 8, 26, 19, 30))
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        family, anya, bot,
    )

    await session.refresh(entry)
    local = tu.to_local(entry.due_at, MSK)
    assert local.date() == tu.local_today(MSK) + timedelta(days=1)
    assert local.time() == time(19, 30)  # время суток не поехало


@pytest.mark.asyncio
async def test_move_survives_the_dst_switch(session, family, anya):
    """Перенос через перевод часов: 19:00 обязано остаться 19:00.

    В ночь на 25 октября 2026 Европа переходит на зимнее время, и сутки там
    длиннее на час. Сдвиг `timedelta` поверх наивного UTC дал бы 18:00 —
    поэтому день меняется в локальном времени, а не в UTC.
    """
    berlin = "Europe/Berlin"
    family.tz = berlin
    await session.commit()
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="через перевод часов",
        due_at=tu.to_utc(datetime(2026, 10, 24, 19, 0), berlin),  # ещё CEST
    )
    before = entry.due_at

    moved = await handler._move(session, entry, family, date(2026, 10, 25))  # уже CET

    assert tu.to_local(moved.due_at, berlin).time() == time(19, 0)
    # Наивный сдвиг на сутки дал бы ровно это — и уехал бы на час
    assert moved.due_at != before + timedelta(days=1)


@pytest.mark.asyncio
async def test_move_of_a_closed_entry_changes_nothing(
    session, family, anya, bot, overdue
):
    entry = await overdue()
    before = entry.due_at
    await repo.complete_entry(session, entry.id, family.id, anya.id)
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        family, anya, bot,
    )

    await session.refresh(entry)
    assert entry.due_at == before
    assert call.alert == texts.REVIEW_STALE


@pytest.mark.asyncio
async def test_move_from_another_chat_is_refused(session, family, anya, bot, overdue):
    """Изоляция по семье: колбэк из чужого чата не трогает чужую запись."""
    entry = await overdue()
    before = entry.due_at
    alien = SimpleNamespace(id=family.id + 1, chat_id=-999, tz=MSK)
    call = FakeCall(alien.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        alien, anya, bot,
    )

    await session.refresh(entry)
    assert entry.due_at == before
    assert call.alert == texts.REVIEW_STALE


@pytest.mark.asyncio
async def test_another_chat_never_sees_the_title(session, family, anya, bot, overdue):
    """Проверка на чтении, а не только на записи.

    `repo.reschedule_entry` стережёт саму правку, но экран выбора дня
    показывает **заголовок** записи — и без проверки в хендлере чужой чат
    прочитал бы его, ничего не меняя.
    """
    entry = await overdue("секрет чужой семьи")
    alien = SimpleNamespace(id=family.id + 1, chat_id=-999, tz=MSK)
    call = FakeCall(alien.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="move", entry_id=entry.id), session, alien, anya, bot
    )

    assert "секрет" not in call.screen
    assert call.alert == texts.REVIEW_STALE


# --- напоминание после переноса ----------------------------------------------


async def _move_and_remind(session, family, anya, bot, entry, minutes, action="rem"):
    call = FakeCall(family.chat_id)
    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        family, anya, bot,
    )
    await session.refresh(entry)
    await handler.tap(
        call, kb.ReviewCB(action=action, entry_id=entry.id, value=minutes), session,
        family, anya, bot,
    )
    return call


@pytest.mark.asyncio
async def test_reminder_is_created_before_the_new_due(
    session, family, anya, bot, overdue
):
    entry = await overdue(due=_msk(2026, 8, 26, 19, 0))
    await _move_and_remind(session, family, anya, bot, entry, 60)

    reminders = list(await session.scalars(select(Reminder).where(Reminder.entry_id == entry.id)))
    assert len(reminders) == 1
    assert reminders[0].fire_at == entry.due_at - timedelta(hours=1)
    assert reminders[0].rrule is None


@pytest.mark.asyncio
async def test_no_reminder_asked_means_none_created(
    session, family, anya, bot, overdue
):
    entry = await overdue()
    await _move_and_remind(session, family, anya, bot, entry, 0, action="norem")

    assert list(await session.scalars(select(Reminder))) == []


@pytest.mark.asyncio
async def test_allday_counts_from_the_anchor(session, family, anya, bot, overdue):
    """У записи «на весь день» срок — полночь, «за 15 минут» дало бы 23:45."""
    entry = await overdue(due=_msk(2026, 8, 26), all_day=True)
    await _move_and_remind(session, family, anya, bot, entry, 0)

    reminder = list(await session.scalars(select(Reminder)))[0]
    assert tu.to_local(reminder.fire_at, MSK).time() == time(9, 0)


@pytest.mark.asyncio
async def test_old_one_off_reminder_is_switched_off(
    session, family, anya, bot, overdue
):
    """Иначе оно выстрелит по прежнему `fire_at` — догонкой в ближайший тик."""
    entry = await overdue()
    old = await repo.create_reminder(
        session, family_id=family.id, created_by=anya.id, text="старое",
        fire_at=_msk(2026, 8, 26, 18, 45), entry_id=entry.id,
    )
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        family, anya, bot,
    )

    await session.refresh(old)
    assert old.active is False


@pytest.mark.asyncio
async def test_recurring_reminder_survives_the_move(
    session, family, anya, bot, overdue
):
    """Тихо убить серию хуже, чем оставить её как есть."""
    entry = await overdue()
    series = await repo.create_reminder(
        session, family_id=family.id, created_by=anya.id, text="каждый вторник",
        fire_at=_msk(2026, 9, 1, 19, 0), entry_id=entry.id,
        rrule="FREQ=WEEKLY;BYDAY=TU",
    )
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=entry.id, value=1), session,
        family, anya, bot,
    )

    await session.refresh(series)
    assert series.active is True


# --- «другая дата» реплаем ----------------------------------------------------


@pytest.mark.asyncio
async def test_other_day_moves_by_reply(session, family, anya, bot, overdue):
    entry = await overdue(due=_msk(2026, 8, 26, 19, 30))
    call = FakeCall(family.chat_id)
    await handler.tap(
        call, kb.ReviewCB(action="other", entry_id=entry.id), session,
        family, anya, bot,
    )
    assert texts.REVIEW_ASK_DATE in call.screen

    await handler.take_day(
        FakeReply(family.chat_id, "через неделю"), session, family, bot
    )

    await session.refresh(entry)
    local = tu.to_local(entry.due_at, MSK)
    assert local.date() > tu.local_today(MSK)
    assert local.time() == time(19, 30)  # время суток осталось своим


@pytest.mark.asyncio
async def test_unparsed_reply_keeps_waiting(session, family, anya, bot, overdue):
    entry = await overdue()
    call = FakeCall(family.chat_id)
    await handler.tap(
        call, kb.ReviewCB(action="other", entry_id=entry.id), session,
        family, anya, bot,
    )

    reply = FakeReply(family.chat_id, "ыва")
    await handler.take_day(reply, session, family, bot)

    assert reply.replies == [texts.REVIEW_BAD_DATE]
    assert (family.chat_id, 500) in handler._pending  # ждём ещё раз


# --- панель -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_is_woken_by_close_and_by_move(
    session, family, anya, bot, overdue, monkeypatch
):
    """Иначе панель дня останется с закрытой или перенесённой записью."""
    calls: list[int] = []
    monkeypatch.setattr(
        handler.panel, "schedule", lambda bot_, family_id, *a: calls.append(family_id)
    )
    first = await overdue("первое")
    second = await overdue("второе")
    call = FakeCall(family.chat_id)

    await handler.tap(
        call, kb.ReviewCB(action="done", entry_id=first.id), session, family, anya, bot
    )
    await handler.tap(
        call, kb.ReviewCB(action="day", entry_id=second.id, value=1), session,
        family, anya, bot,
    )

    assert calls == [family.id, family.id]
