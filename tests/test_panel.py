"""Живая панель дня — этап 2п.

Панель держит состояние в модульных словарях `panel._locks` / `panel._tasks`;
уборка между тестами — в autouse-фикстуре `_panel_state` из `conftest.py`.
Сессию панель открывает сама, поэтому фабрику подменяем monkeypatch'ем — так же,
как тесты делают с тикером.
"""

import asyncio
from datetime import date, datetime

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from bot import texts
from bot.db import repo
from bot.services import panel, sending
from bot.services import timeutil as tu
from tests.conftest import FakeBot

MSK = "Europe/Moscow"
NOW = tu.to_utc(datetime(2026, 8, 27, 12, 0), MSK)
TODAY = date(2026, 8, 27)


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=message)


@pytest.fixture(autouse=True)
def _own_session(monkeypatch, session_maker):
    monkeypatch.setattr(panel, "Session", session_maker)


async def _drain(family_id):
    """Дождаться отложенной задачи, а не спать «на глазок».

    Сон на фиксированные миллисекунды делает тест мигающим: под нагрузкой
    задача не успевает открыть сессию и сходить в базу.
    """
    task = panel._tasks.get(family_id)
    if task is not None:
        await task


async def _reload(session, family):
    """Панель пишет через свою сессию — объект теста об этом не узнает сам."""
    await session.refresh(family)
    return family


async def _entry(session, family, anya, title="Забрать посылку"):
    """Запись на сегодня: без `due_at` она в день не попадёт и панель её не покажет."""
    return await repo.create_entry(
        session,
        family_id=family.id,
        author_id=anya.id,
        kind="task",
        title=title,
        due_at=NOW,
    )


# --- первая панель и обычная перерисовка -------------------------------------


@pytest.mark.asyncio
async def test_first_refresh_sends_and_pins(session, family, anya, bot):
    await _entry(session, family, anya)

    await panel.refresh(bot, family.id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1
    assert texts.PANEL_HEADER in bot.texts[0]
    assert "Забрать посылку" in bot.texts[0]
    assert family.panel_message_id is not None
    assert family.panel_day == TODAY
    assert bot.pinned == [family.panel_message_id]


@pytest.mark.asyncio
async def test_second_refresh_edits_instead_of_sending(session, family, anya, bot):
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id

    await _entry(session, family, anya)
    await panel.refresh(bot, family.id, last_message_id=first_id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1, "вторая панель не выпускается — правится первая"
    assert len(bot.edited) == 1
    assert bot.edited[0][1] == first_id
    assert family.panel_message_id == first_id


@pytest.mark.asyncio
async def test_empty_day_still_gets_a_panel(session, family, bot):
    """В отличие от дайджеста, панель на пустом дне не молчит: она постоянная."""
    await panel.refresh(bot, family.id, now=NOW)

    assert len(bot.sent) == 1
    assert texts.EMPTY_TODAY in bot.texts[0]


# --- дебаунс и лок ------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_changes_in_a_row_give_one_refresh(monkeypatch, session, family, bot):
    """Критерий 2п.2: две правки подряд дают одно сообщение, а не два."""
    monkeypatch.setattr(panel.settings, "panel_debounce_seconds", 0.01)

    panel.schedule(bot, family.id)
    panel.schedule(bot, family.id)
    await _drain(family.id)

    assert len(bot.sent) == 1
    assert len(bot.edited) == 0


@pytest.mark.asyncio
async def test_lock_serialises_concurrent_refreshes(session, family, bot):
    """Двое тапнули одновременно: вторая перерисовка правит, а не шлёт вторую панель."""
    await asyncio.gather(
        panel.refresh(bot, family.id, now=NOW),
        panel.refresh(bot, family.id, now=NOW),
    )

    assert len(bot.sent) == 1
    assert len(bot.edited) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_debounce(monkeypatch, session, family, bot):
    monkeypatch.setattr(panel.settings, "panel_debounce_seconds", 5)

    panel.schedule(bot, family.id)
    await panel.shutdown()

    assert bot.sent == []
    assert panel._tasks == {}


# --- перевыпуск ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_republished_after_max_messages(monkeypatch, session, family, bot):
    """Критерий 2п.3: панель уехала вверх — выпускаем новую, старую открепляем."""
    monkeypatch.setattr(panel.settings, "panel_max_messages", 5)
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id

    await panel.refresh(bot, family.id, last_message_id=first_id + 6, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 2
    assert bot.edited == []
    assert bot.unpinned == [first_id]
    assert family.panel_message_id != first_id
    assert bot.pinned == [first_id, family.panel_message_id]


@pytest.mark.asyncio
async def test_not_republished_while_panel_is_close(monkeypatch, session, family, bot):
    monkeypatch.setattr(panel.settings, "panel_max_messages", 5)
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id

    await panel.refresh(bot, family.id, last_message_id=first_id + 1, now=NOW)

    assert len(bot.sent) == 1
    assert len(bot.edited) == 1


@pytest.mark.asyncio
async def test_republished_on_a_new_local_day(session, family, bot):
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id

    # 00:30 по Москве следующего дня: в UTC это ещё 27-е, и панель, сравнивающая
    # дни в UTC, решила бы, что перевыпускать нечего
    tomorrow = tu.to_utc(datetime(2026, 8, 28, 0, 30), MSK)
    await panel.refresh(bot, family.id, last_message_id=first_id, now=tomorrow)
    await _reload(session, family)

    assert len(bot.sent) == 2
    assert family.panel_day == date(2026, 8, 28)
    assert bot.unpinned == [first_id]


@pytest.mark.asyncio
async def test_stale_panel_is_refreshed_by_the_ticker(monkeypatch, session, family, bot):
    monkeypatch.setattr(panel.settings, "panel_debounce_seconds", 0.01)
    await repo.set_panel(session, family, 500, date(2026, 8, 26))

    await panel.refresh_stale(bot, session, NOW)
    await _drain(family.id)

    assert len(bot.sent) == 1, "вчерашняя панель перевыпущена"


@pytest.mark.asyncio
async def test_ticker_leaves_a_fresh_panel_alone(session, family, bot):
    """В обычный тик панель не трогаем вовсе — ни одного запроса к Telegram."""
    await repo.set_panel(session, family, 500, TODAY)

    await panel.refresh_stale(bot, session, NOW)

    assert bot.sent == [] and bot.edited == []
    assert panel._tasks == {}


@pytest.mark.asyncio
async def test_ticker_does_not_create_a_missing_panel(session, family, bot):
    await panel.refresh_stale(bot, session, NOW)
    assert panel._tasks == {}


# --- ошибки Telegram ----------------------------------------------------------


@pytest.mark.asyncio
async def test_not_modified_does_not_republish(session, family, bot):
    """Холостая правка — норма панели, а не повод плодить новую."""
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id
    bot._fail_on_edit = {0: _bad_request("Bad Request: message is not modified")}

    await panel.refresh(bot, family.id, last_message_id=first_id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1
    assert family.panel_message_id == first_id


@pytest.mark.asyncio
async def test_deleted_panel_is_republished(session, family, bot):
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id
    bot._fail_on_edit = {0: _bad_request("Bad Request: message to edit not found")}

    await panel.refresh(bot, family.id, last_message_id=first_id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 2
    assert family.panel_message_id != first_id


@pytest.mark.asyncio
async def test_broken_text_does_not_republish(session, family, bot):
    """Иначе неотправляемый текст дал бы бесконечный поток новых панелей."""
    await panel.refresh(bot, family.id, now=NOW)
    first_id = (await _reload(session, family)).panel_message_id
    bot._fail_on_edit = {0: _bad_request("Bad Request: message is too long")}

    await panel.refresh(bot, family.id, last_message_id=first_id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1
    assert family.panel_message_id == first_id


@pytest.mark.asyncio
async def test_unsendable_panel_does_not_loop_every_tick(session, family, bot):
    """Битый текст не должен уходить в Telegram каждую минуту до полуночи.

    Панель за вчера + отказ на отправке = тикер видит вчерашний `panel_day`
    и планирует перевыпуск снова и снова. Поэтому день помечается отработанным.
    """
    await repo.set_panel(session, family, 500, date(2026, 8, 26))
    bot._fail_on = {0: _bad_request("Bad Request: message is too long")}

    await panel.refresh(bot, family.id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1
    assert family.panel_message_id == 500, "старая панель остаётся, новой не вышло"
    assert family.panel_day == TODAY, "иначе следующий тик отправит то же самое"

    await panel.refresh_stale(bot, session, NOW)
    assert panel._tasks == {}, "тикер больше не планирует перевыпуск"


@pytest.mark.asyncio
async def test_flood_control_is_retried_on_the_next_tick(session, family, bot):
    """А вот флуд-контроль — временный: день помечать нельзя."""
    await repo.set_panel(session, family, 500, date(2026, 8, 26))
    bot._fail_on = {0: TelegramRetryAfter(method=None, message="flood", retry_after=5)}

    await panel.refresh(bot, family.id, now=NOW)
    await _reload(session, family)

    assert family.panel_day == date(2026, 8, 26), "день остаётся вчерашним — тикер повторит"


@pytest.mark.asyncio
async def test_panel_survives_missing_pin_rights(session, family, bot):
    """Без прав админа панель живёт незакреплённой, а не выпускается заново."""
    bot._fail_on_pin = {0: _bad_request("Bad Request: not enough rights to pin a message")}

    await panel.refresh(bot, family.id, now=NOW)
    await _reload(session, family)

    assert len(bot.sent) == 1
    assert family.panel_message_id is not None, (
        "id записан — иначе следующая правка выпустит вторую панель"
    )


@pytest.mark.asyncio
async def test_kicked_bot_forgets_the_panel(session, family, bot):
    await panel.refresh(bot, family.id, now=NOW)
    bot._fail_on_edit = {0: TelegramForbiddenError(method=None, message="kicked")}
    bot._fail_on = {1: TelegramForbiddenError(method=None, message="kicked")}

    await panel.refresh(bot, family.id, last_message_id=family.panel_message_id, now=NOW)
    await _reload(session, family)

    assert family.panel_message_id is None
    assert family.panel_day is None


# --- классификация ошибок редактирования (без БД) -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, expected",
    [
        ("Bad Request: message is not modified", sending.OK),
        ("Bad Request: message to edit not found", sending.NOT_FOUND),
        ("Bad Request: message can't be edited", sending.NOT_FOUND),
        ("Bad Request: message is too long", sending.BROKEN),
    ],
)
async def test_edit_classifies_bad_requests(family, message, expected):
    bot = FakeBot(fail_on_edit={0: _bad_request(message)})
    assert await sending.edit(bot, family, 1, "текст") == expected


@pytest.mark.asyncio
async def test_edit_treats_network_failure_as_retry(family):
    bot = FakeBot(fail_on_edit={0: RuntimeError("сеть отвалилась")})
    assert await sending.edit(bot, family, 1, "текст") == sending.RETRY
