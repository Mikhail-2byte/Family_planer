"""Тикер: выборка созревших, догонка, повторяемость — этап 2.1–2.4."""

from datetime import datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError

from bot.config import settings
from bot.db import repo
from bot.services import ticker
from bot.services import timeutil as tu

MSK = "Europe/Moscow"
NOW = datetime(2026, 8, 27, 9, 0)  # четверг, 12:00 по Москве


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


async def _add(session, family, anya, *, text, fire_at, rrule=None, entry_id=None):
    return await repo.create_reminder(
        session,
        family_id=family.id,
        created_by=anya.id,
        text=text,
        fire_at=fire_at,
        rrule=rrule,
        entry_id=entry_id,
    )


# --- выборка созревших -------------------------------------------------------


@pytest.mark.asyncio
async def test_due_takes_only_ripe_and_active(session, family, anya):
    await _add(session, family, anya, text="пора", fire_at=NOW - timedelta(minutes=1))
    await _add(session, family, anya, text="рано", fire_at=NOW + timedelta(hours=1))
    sent = await _add(session, family, anya, text="уже было", fire_at=NOW - timedelta(hours=2))
    await repo.mark_reminder_sent(session, sent, NOW)
    off = await _add(session, family, anya, text="выключено", fire_at=NOW - timedelta(hours=2))
    await repo.deactivate_reminder(session, off)

    due = await repo.due_reminders(session, NOW)
    assert [r.text for r in due] == ["пора"]


@pytest.mark.asyncio
async def test_due_skips_reminder_of_closed_entry(session, family, anya):
    """`complete_entry` не трогает reminders — отсев обязан быть в выборке."""
    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="Купить хлеб"
    )
    await _add(
        session, family, anya, text="Купить хлеб",
        fire_at=NOW - timedelta(minutes=1), entry_id=entry.id,
    )
    assert len(await repo.due_reminders(session, NOW)) == 1

    await repo.complete_entry(session, entry.id, family.id, anya.id)
    assert await repo.due_reminders(session, NOW) == []


# --- догонка: три порога -----------------------------------------------------


@pytest.mark.parametrize(
    "delay, expected",
    [
        (timedelta(seconds=0), ticker.SILENT),
        (timedelta(minutes=9, seconds=59), ticker.SILENT),
        (timedelta(minutes=10), ticker.LATE),
        (timedelta(hours=11, minutes=59), ticker.LATE),
        (timedelta(hours=12), ticker.SUMMARY),
        (timedelta(days=3), ticker.SUMMARY),
    ],
)
def test_classify_lateness_at_thresholds(delay, expected):
    assert (
        ticker.classify_lateness(
            NOW - delay, NOW, silent_min=10, summary_hours=12
        )
        == expected
    )


@pytest.mark.asyncio
async def test_long_outage_collapses_into_one_summary(session, family, anya, bot):
    for i in range(5):
        await _add(
            session, family, anya,
            text=f"дело {i}", fire_at=NOW - timedelta(days=2, hours=i),
        )

    await ticker.send_due_reminders(bot, session, NOW)

    assert len(bot.sent) == 1, "пять пропущенных должны схлопнуться в одно сообщение"
    assert "Пока меня не было" in bot.texts[0]
    assert "пропущено: 5" in bot.texts[0]
    assert await repo.due_reminders(session, NOW) == []


@pytest.mark.asyncio
async def test_moderate_delay_gets_a_note(session, family, anya, bot):
    await _add(session, family, anya, text="позвонить маме", fire_at=NOW - timedelta(hours=3))
    await ticker.send_due_reminders(bot, session, NOW)

    assert len(bot.sent) == 1
    assert "было запланировано на" in bot.texts[0]


@pytest.mark.asyncio
async def test_on_time_reminder_has_no_note(session, family, anya, bot):
    await _add(session, family, anya, text="забрать посылку", fire_at=NOW - timedelta(minutes=1))
    await ticker.send_due_reminders(bot, session, NOW)

    assert bot.texts == ["🔔 забрать посылку"]


@pytest.mark.asyncio
async def test_reminder_text_is_escaped(session, family, anya, bot):
    await _add(session, family, anya, text="<b>жирный</b> & хитрый", fire_at=NOW)
    await ticker.send_due_reminders(bot, session, NOW)
    assert "&lt;b&gt;жирный&lt;/b&gt; &amp; хитрый" in bot.texts[0]


# --- устойчивость ------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_send_is_retried_next_tick(session, family, anya):
    from tests.conftest import FakeBot

    await _add(session, family, anya, text="важное", fire_at=NOW - timedelta(minutes=1))

    flaky = FakeBot(fail_on={0: RuntimeError("сеть отвалилась")})
    await ticker.send_due_reminders(flaky, session, NOW)
    assert len(await repo.due_reminders(session, NOW)) == 1, "не дошло — не помечаем"

    ok = FakeBot()
    await ticker.send_due_reminders(ok, session, NOW)
    assert len(ok.sent) == 1
    assert await repo.due_reminders(session, NOW) == []


@pytest.mark.asyncio
async def test_kicked_from_chat_deactivates_reminder(session, family, anya):
    from tests.conftest import FakeBot

    await _add(session, family, anya, text="в никуда", fire_at=NOW - timedelta(minutes=1))
    kicked = FakeBot(fail_on={0: TelegramForbiddenError(method=None, message="kicked")})

    await ticker.send_due_reminders(kicked, session, NOW)
    assert await repo.due_reminders(session, NOW) == []


@pytest.mark.asyncio
async def test_one_broken_send_does_not_stop_the_rest(session, family, anya):
    from tests.conftest import FakeBot

    for i in range(3):
        await _add(
            session, family, anya,
            text=f"дело {i}", fire_at=NOW - timedelta(minutes=3 - i),
        )

    flaky = FakeBot(fail_on={1: RuntimeError("сеть моргнула")})
    await ticker.send_due_reminders(flaky, session, NOW)

    assert len(flaky.sent) == 3, "сбой второго не должен мешать третьему"
    left = await repo.due_reminders(session, NOW)
    assert len(left) == 1, "неотправленным остаётся ровно то, что упало"


# --- повторяемость -----------------------------------------------------------


def test_next_fire_at_moves_to_following_week():
    fire_at = _msk(2026, 8, 25, 19, 0)  # вторник 19:00 по Москве
    following = ticker.next_fire_at("FREQ=WEEKLY;BYDAY=TU", fire_at, fire_at, MSK)
    assert tu.to_local(following, MSK) == datetime(2026, 9, 1, 19, 0)


def test_next_fire_at_skips_the_whole_outage():
    """ПК был выключен три недели — прыгаем в будущее, а не догоняем по шагу."""
    fire_at = _msk(2026, 8, 25, 19, 0)
    now = _msk(2026, 9, 15, 10, 0)
    following = ticker.next_fire_at("FREQ=WEEKLY;BYDAY=TU", fire_at, now, MSK)
    assert tu.to_local(following, MSK) == datetime(2026, 9, 15, 19, 0)
    assert following > now


def test_next_fire_at_keeps_local_hour_across_dst():
    """В Берлине 25.10.2026 переводят часы: 19:00 должно остаться 19:00."""
    berlin = "Europe/Berlin"
    fire_at = tu.to_utc(datetime(2026, 10, 20, 19, 0), berlin)  # вторник до перевода
    following = ticker.next_fire_at("FREQ=WEEKLY;BYDAY=TU", fire_at, fire_at, berlin)
    assert tu.to_local(following, berlin) == datetime(2026, 10, 27, 19, 0)


def test_next_fire_at_returns_none_on_garbage():
    assert ticker.next_fire_at("МУСОР", NOW, NOW, MSK) is None
    assert ticker.next_fire_at("", NOW, NOW, MSK) is None


def test_next_fire_at_returns_none_when_series_ends():
    fire_at = _msk(2026, 8, 25, 19, 0)
    assert ticker.next_fire_at("FREQ=WEEKLY;COUNT=1", fire_at, fire_at, MSK) is None


@pytest.mark.asyncio
async def test_recurring_reminder_is_rescheduled_not_closed(session, family, anya, bot):
    reminder = await _add(
        session, family, anya,
        text="тренировка", fire_at=_msk(2026, 8, 25, 19, 0), rrule="FREQ=WEEKLY;BYDAY=TU",
    )
    now = _msk(2026, 8, 25, 19, 0) + timedelta(seconds=30)

    await ticker.send_due_reminders(bot, session, now)

    assert len(bot.sent) == 1
    await session.refresh(reminder)
    assert reminder.sent_at is None, "у повторяющегося отработку помечает fire_at"
    assert reminder.active is True
    assert tu.to_local(reminder.fire_at, MSK) == datetime(2026, 9, 1, 19, 0)
    assert await repo.due_reminders(session, now) == []


@pytest.mark.asyncio
async def test_broken_rrule_is_deactivated(session, family, anya, bot):
    """Иначе напоминание с мусором в правиле сработает на каждом тике."""
    reminder = await _add(
        session, family, anya, text="сломано", fire_at=NOW, rrule="НЕ-ПРАВИЛО",
    )
    await ticker.send_due_reminders(bot, session, NOW)

    await session.refresh(reminder)
    assert reminder.active is False
    assert await repo.due_reminders(session, NOW) == []


@pytest.mark.asyncio
async def test_summary_lists_at_most_ten(session, family, anya, bot):
    from bot import texts

    for i in range(14):
        await _add(session, family, anya, text=f"дело {i}", fire_at=NOW - timedelta(days=2))

    await ticker.send_due_reminders(bot, session, NOW)
    body = bot.texts[0]
    assert "пропущено: 14" in body
    assert body.count("• ") == texts.MAX_SUMMARY_ITEMS
    assert "и ещё 4" in body


@pytest.mark.asyncio
async def test_ticker_uses_settings_thresholds(session, family, anya, bot, monkeypatch):
    """Пороги берутся из конфига, а не зашиты числом."""
    monkeypatch.setattr(settings, "late_silent_min", 1)
    await _add(session, family, anya, text="дело", fire_at=NOW - timedelta(minutes=5))
    await ticker.send_due_reminders(bot, session, NOW)
    assert "было запланировано на" in bot.texts[0]


def test_next_fire_at_ignores_dtstart_inside_the_rule():
    """`rrulestr` при DTSTART в строке игнорирует аргумент `dtstart`.

    Без срезки якорем становилась дата из правила, и «каждый вторник в 19:00»
    уезжало во вторник в 00:00 — время суток серии терялось молча.
    """
    fire_at = _msk(2026, 8, 25, 19, 0)  # вторник 19:00 по Москве
    with_dtstart = "DTSTART:20200101T000000\nRRULE:FREQ=WEEKLY;BYDAY=TU"

    following = ticker.next_fire_at(with_dtstart, fire_at, fire_at, MSK)
    assert tu.to_local(following, MSK) == datetime(2026, 9, 1, 19, 0)


def test_next_fire_at_returns_none_when_only_dtstart_is_left():
    """После срезки правила не остаётся — гасим, а не срабатываем каждый тик."""
    assert ticker.next_fire_at("DTSTART:20200101T000000", NOW, NOW, MSK) is None
