"""Утренний дайджест — этап 2.6."""

from datetime import date, datetime, timedelta

import pytest

from bot import texts
from bot.db import repo
from bot.services import digest
from bot.services import timeutil as tu

MSK = "Europe/Moscow"


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


# --- когда пора --------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_due_before_digest_time(session, family):
    family.digest_time = "08:00"
    assert digest.is_due(family, _msk(2026, 8, 27, 7, 59)) is False


@pytest.mark.asyncio
async def test_due_right_at_digest_time(session, family):
    family.digest_time = "08:00"
    assert digest.is_due(family, _msk(2026, 8, 27, 8, 0)) is True


@pytest.mark.asyncio
async def test_not_due_twice_in_one_day(session, family):
    family.digest_time = "08:00"
    family.last_digest_on = date(2026, 8, 27)
    assert digest.is_due(family, _msk(2026, 8, 27, 9, 0)) is False
    # ...но назавтра снова пора
    assert digest.is_due(family, _msk(2026, 8, 28, 9, 0)) is True


@pytest.mark.asyncio
async def test_broken_digest_time_does_not_explode(session, family):
    """Одна кривая строка в БД не должна останавливать дайджест всем семьям."""
    family.digest_time = "восемь утра"
    assert digest.is_due(family, _msk(2026, 8, 27, 12, 0)) is False


# --- содержимое --------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_day_reports_emptiness(session, family):
    text, has_content = await digest.build_day(session, family, _msk(2026, 8, 27, 8, 0))
    assert has_content is False
    assert texts.EMPTY_TODAY in text


@pytest.mark.asyncio
async def test_build_day_collects_today_and_overdue(session, family, anya):
    now = _msk(2026, 8, 27, 8, 0)
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Старый долг", due_at=_msk(2026, 8, 20, 10, 0),
    )

    text, has_content = await digest.build_day(session, family, now)
    assert has_content is True
    assert "Встреча" in text and "Старый долг" in text
    assert texts.HEADER_OVERDUE in text


@pytest.mark.asyncio
async def test_long_overdue_list_stays_within_telegram_limit(session, family, anya):
    """Просрочка копится годами и без потолка перерастает 4096 символов.

    Дальше `sending.deliver` вернул бы BROKEN, а `_send_one` всё равно проставил
    бы `last_digest_on` — сводка пропадала бы молча каждое утро.
    """
    now = _msk(2026, 8, 27, 8, 0)
    for i in range(60):
        await repo.create_entry(
            session, family_id=family.id, author_id=anya.id, kind="task",
            title=f"просроченная задача номер {i} про молоко",
            due_at=now - timedelta(days=30 + i),
        )

    text, has_content = await digest.build_day(session, family, now)

    assert has_content is True
    assert len(text) < 4096
    assert text.count("⚠️") == 1  # только заголовок блока, строки обрезаны
    assert texts.MORE_ITEMS.format(count=60 - texts.MAX_DAY_ITEMS) in text


# --- отправка ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_goes_out_once_a_day(session, family, anya, bot):
    family.digest_time = "08:00"
    await session.commit()
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )

    now = _msk(2026, 8, 27, 8, 0)
    await digest.send_pending(bot, session, now)
    assert len(bot.sent) == 1
    assert texts.DIGEST_HEADER in bot.texts[0]

    # Повторный тик в тот же день молчит — критерий пункта 2.6
    await digest.send_pending(bot, session, now + timedelta(minutes=1))
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_empty_day_still_gets_a_digest_but_without_a_ping(
    session, family, bot
):
    """С этапа 11 пустой день не молчит — но и не будит.

    До уборки чата молчание было верным: чат жил своей жизнью, и «на сегодня
    ничего не запланировано» каждое утро было бы шумом. Теперь уборка стирает
    всё выше сводки, и молчание оставило бы человека с пустым чатом без
    единого объяснения, куда всё делось.

    Звук при этом снят: `has_content` сменил работу с «слать или не слать» на
    «будить или не будить». Отметка дня — как и была, иначе каждый тик до
    полуночи пересобирает сводку заново.
    """
    family.digest_time = "08:00"
    await session.commit()

    now = _msk(2026, 8, 27, 8, 0)
    await digest.send_pending(bot, session, now)

    assert len(bot.sent) == 1
    assert texts.EMPTY_TODAY in bot.texts[0]
    assert bot.kwargs[0]["disable_notification"] is True
    await session.refresh(family)
    assert family.last_digest_on == date(2026, 8, 27)


@pytest.mark.asyncio
async def test_late_digest_says_so(session, family, anya, bot):
    """ПК включили в обед — сводка уходит, но с оговоркой."""
    family.digest_time = "08:00"
    await session.commit()
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )

    await digest.send_pending(bot, session, _msk(2026, 8, 27, 14, 0))
    assert texts.DIGEST_LATE_NOTE in bot.texts[0]


@pytest.mark.asyncio
async def test_on_time_digest_has_no_note(session, family, anya, bot):
    family.digest_time = "08:00"
    await session.commit()
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )

    await digest.send_pending(bot, session, _msk(2026, 8, 27, 8, 0))
    assert texts.DIGEST_LATE_NOTE not in bot.texts[0]


@pytest.mark.asyncio
async def test_undelivered_digest_is_retried(session, family, anya, bot):
    from tests.conftest import FakeBot

    family.digest_time = "08:00"
    await session.commit()
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )
    now = _msk(2026, 8, 27, 8, 0)

    flaky = FakeBot(fail_on={0: RuntimeError("сеть отвалилась")})
    await digest.send_pending(flaky, session, now)
    await session.refresh(family)
    assert family.last_digest_on is None, "не дошло — отметку не ставим"

    await digest.send_pending(bot, session, now + timedelta(minutes=1))
    assert len(bot.sent) == 1
