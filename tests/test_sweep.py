"""Утренняя уборка чата (этап 11).

Уборка удаляет **диапазоном id**, а не списком запомненных сообщений, и почти
все тесты здесь стерегут именно границы этого диапазона: ошибка в них не даёт
исключения, а тихо оставляет чат грязным или, наоборот, сносит лишнее.

`plan` проверяется без единого фейка — она чистая, как `parsing.normalize` и
`export.to_ics`.
"""

from datetime import date, datetime, timedelta

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from bot.db import repo
from bot.services import digest, sweep
from bot.services import timeutil as tu
from tests.conftest import FakeBot

MSK = "Europe/Moscow"


def _msk(y, m, d, hh=0, mm=0) -> datetime:
    return tu.to_utc(datetime(y, m, d, hh, mm), MSK)


def _bad(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=message)


# --- plan: границы диапазона --------------------------------------------------


def test_plan_cuts_the_range_into_hundreds():
    batches = sweep.plan(swept_upto=100, anchor=351, window=1000)

    assert [len(b) for b in batches] == [100, 100, 50]
    assert batches[0][0] == 101
    assert batches[-1][-1] == 350


def test_plan_stops_one_below_the_anchor():
    """Сама сводка и всё, что пришло после неё, обязаны выжить.

    Разбор незакрытого уходит **после** сводки, значит его id выше якоря —
    попади он под нож, кнопки «закрыть/перенести» исчезали бы вместе с ним.
    """
    batches = sweep.plan(swept_upto=200, anchor=205, window=1000)

    assert [i for b in batches for i in b] == [201, 202, 203, 204]


def test_plan_never_yields_ids_below_one():
    """У молодого чата якорь меньше окна, и range уехал бы в отрицательные id."""
    batches = sweep.plan(swept_upto=None, anchor=11, window=1000)

    assert [i for b in batches for i in b] == list(range(1, 11))


def test_plan_looks_back_no_further_than_the_window():
    """Один потолок закрывает сразу два случая, и это его смысл.

    Первый запуск (`swept_upto is None`) — иначе бот полез бы с первого id чата.
    Догонка после простоя — ноутбук спит от батареи и при закрытой крышке, и
    без потолка диапазон рос бы неограниченно.
    """
    first_run = sweep.plan(swept_upto=None, anchor=5000, window=100)
    after_outage = sweep.plan(swept_upto=10, anchor=5000, window=100)

    assert [i for b in first_run for i in b] == list(range(4900, 5000))
    assert [i for b in after_outage for i in b] == list(range(4900, 5000))


def test_plan_is_empty_when_there_is_nothing_new():
    assert sweep.plan(swept_upto=350, anchor=351, window=1000) == []


def test_zero_window_switches_the_sweep_off():
    """Идиома проекта: выключатель — число, а не флаг (`BACKUP_KEEP=0`)."""
    assert sweep.plan(swept_upto=None, anchor=5000, window=0) == []


# --- сквозная уборка ----------------------------------------------------------


@pytest.fixture
def morning(session, family):
    """Семье пора слать сводку, чат вычищен до заданного id."""

    async def setup(swept_upto=100):
        family.digest_time = "08:00"
        family.tz = MSK
        family.swept_upto = swept_upto
        await session.commit()
        return _msk(2026, 8, 27, 8, 0)

    return setup


@pytest.mark.asyncio
async def test_morning_sweep_wipes_everything_below_the_digest(
    session, family, anya, bot, morning
):
    """FakeBot нумерует с сотни, поэтому якорь сводки — 101."""
    now = await morning(swept_upto=50)
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="event",
        title="Встреча", due_at=_msk(2026, 8, 27, 19, 0),
    )

    await digest.send_pending(bot, session, now)

    assert bot.wiped == list(range(51, 101))


@pytest.mark.asyncio
async def test_watermark_stops_one_below_the_anchor(
    session, family, anya, bot, morning
):
    """Знак — `anchor − 1`, и это условие работоспособности, а не мелочь.

    При знаке, равном самой сводке, завтрашний диапазон начнётся **выше** неё, и
    вчерашняя сводка не удалится никогда. Через месяц в чате тридцать «Доброе
    утро!» — ровно то, от чего уходим.
    """
    now = await morning(swept_upto=50)
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Дело", due_at=_msk(2026, 8, 27, 10, 0),
    )

    await digest.send_pending(bot, session, now)

    await session.refresh(family)
    assert family.swept_upto == 100
    assert 101 not in bot.wiped, "сводку не сносим"
    assert 100 in bot.wiped


@pytest.mark.asyncio
async def test_yesterdays_digest_is_gone_the_next_morning(
    session, family, anya, bot, morning
):
    """Та же проверка глазами владельца: два утра подряд — одна сводка в чате."""
    now = await morning(swept_upto=100)
    await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task",
        title="Дело", due_at=_msk(2026, 8, 27, 10, 0),
    )
    await digest.send_pending(bot, session, now)
    yesterday_digest = 101

    await digest.send_pending(bot, session, now + timedelta(days=1))

    assert yesterday_digest in bot.wiped


@pytest.mark.asyncio
async def test_no_anchor_means_no_sweep(session, family, morning):
    """Текст не приняли — чат не трогаем: стереть день, не показав сводку, нельзя."""
    now = await morning(swept_upto=50)
    broken = FakeBot(fail_on={0: _bad("message is too long")})

    await digest.send_pending(broken, session, now)

    assert broken.wiped == []
    await session.refresh(family)
    assert family.swept_upto == 50, "знак не двигаем"
    assert family.last_digest_on == date(2026, 8, 27), "но день помечаем"


@pytest.mark.asyncio
async def test_failed_sweep_does_not_duplicate_the_digest(
    session, family, bot, morning, monkeypatch
):
    """Уборка стоит после отметки дня — и это не вкусовщина.

    Встань она раньше и упади, `send_pending` проглотит исключение, день не
    пометится, и на следующем тике сводка уйдёт снова. И так каждую минуту до
    полуночи: 1400 сводок за сутки.
    """
    now = await morning(swept_upto=50)

    async def boom(*args, **kwargs):
        raise RuntimeError("уборка сорвалась")

    monkeypatch.setattr(sweep, "_run", boom)

    await digest.send_pending(bot, session, now)
    await digest.send_pending(bot, session, now + timedelta(minutes=1))

    assert len(bot.sent) == 1, "сводка ушла ровно один раз"
    await session.refresh(family)
    assert family.last_digest_on == date(2026, 8, 27)


# --- политика ошибок удаления -------------------------------------------------


@pytest.mark.asyncio
async def test_broken_batch_falls_back_one_by_one(session, family, morning):
    """Какое именно сообщение неудаляемо, Telegram не говорит.

    Служебное о создании супергруппы попадёт в самый первый боевой диапазон,
    так что эта ветка отработает на первом же утре, а не когда-нибудь.
    """
    now = await morning(swept_upto=90)
    flaky = FakeBot(fail_on_delete={0: _bad("message to delete not found")})

    await digest.send_pending(flaky, session, now)

    assert flaky.deleted == [list(range(91, 101))]
    assert flaky.deleted_one == list(range(91, 101))


@pytest.mark.asyncio
async def test_undeletable_message_is_not_mistaken_for_missing_rights(
    session, family, morning
):
    """Найдено симуляцией первого боевого утра, до живого прогона.

    «message can't be deleted» означает «вот это конкретное сообщение
    неудаляемо» — служебное о создании супергруппы, слишком старое, — а не
    «прав нет». Приняв его за отказ в правах, уборка останавливалась бы на
    первой же пачке, куда попало служебное сообщение: то есть на первом же утре
    и потом каждое утро. Знак не двигался бы никогда, и вся затея молча не
    делала бы ничего — без единой ошибки в логе.
    """
    now = await morning(swept_upto=90)
    service = FakeBot(fail_on_delete={0: _bad("message can't be deleted")})

    await digest.send_pending(service, session, now)

    assert service.deleted_one == list(range(91, 101)), "откат обязан состояться"
    await session.refresh(family)
    assert family.swept_upto == 100, "и знак обязан сдвинуться"


@pytest.mark.asyncio
async def test_spent_budget_does_not_claim_untouched_messages(
    session, family, morning, monkeypatch
):
    """Вторая находка той же симуляции, и она тише первой.

    Бюджет поштучных общий на утро. Когда он кончался посреди диапазона,
    оставшиеся пачки всё равно объявлялись пройденными — а знак, шагнув за них,
    закрывал им дорогу навсегда: завтрашний диапазон начинается уже выше. На
    чате из 235 сообщений так терялось 135 штук, и заметить это было бы нечем.

    Знак обязан двигаться только за тем, что реально пытались удалить.
    """
    monkeypatch.setattr(sweep, "MAX_SINGLE", 5)
    monkeypatch.setattr(sweep, "SINGLE_PAUSE", 0)
    now = await morning(swept_upto=80)
    # Обе пачки падают: неудаляемое раскидано по разным местам диапазона
    stubborn = FakeBot(
        fail_on_delete={i: _bad("message can't be deleted") for i in range(5)}
    )

    await digest.send_pending(stubborn, session, now)

    await session.refresh(family)
    assert stubborn.deleted_one == [81, 82, 83, 84, 85]
    assert family.swept_upto == 85, "знак — по последнему тронутому, а не по хвосту"


@pytest.mark.asyncio
async def test_missing_rights_do_not_trigger_one_by_one(session, family, morning):
    """Без разделения ошибок это был бы 101 неудачный запрос вместо одного.

    Права могут исчезнуть в любой момент: владелец снял админку, бота
    переприняли в чат.
    """
    now = await morning(swept_upto=90)
    no_rights = FakeBot(
        fail_on_delete={0: _bad("not enough rights to delete messages")}
    )

    await digest.send_pending(no_rights, session, now)

    assert len(no_rights.deleted) == 1
    assert no_rights.deleted_one == []
    await session.refresh(family)
    assert family.swept_upto == 90, "знак не двинут — удалять нечем"


@pytest.mark.asyncio
async def test_forbidden_stops_the_sweep(session, family, morning):
    now = await morning(swept_upto=90)
    kicked = FakeBot(
        fail_on_delete={0: TelegramForbiddenError(method=None, message="kicked")}
    )

    await digest.send_pending(kicked, session, now)

    assert kicked.deleted_one == []


@pytest.mark.asyncio
async def test_flood_control_leaves_the_rest_for_tomorrow(session, family, morning):
    """Знак двигается по фактически пройденным пачкам, а не по задуманным."""
    now = await morning(swept_upto=0)
    flooded = FakeBot(
        fail_on_delete={
            1: TelegramRetryAfter(method=None, message="flood", retry_after=5)
        }
    )

    await digest.send_pending(flooded, session, now)

    await session.refresh(family)
    assert family.swept_upto == 100, "первая пачка прошла, вторая — нет"


@pytest.mark.asyncio
async def test_watermark_moves_over_undeletable_messages(
    session, family, morning, monkeypatch
):
    """Иначе бот долбится в неудаляемое каждое утро до конца времён."""
    monkeypatch.setattr(sweep, "SINGLE_PAUSE", 0)
    now = await morning(swept_upto=90)
    stubborn = FakeBot(
        fail_on_delete={0: _bad("message to delete not found")},
        fail_on_delete_one={i: _bad("message to delete not found") for i in range(10)},
    )

    await digest.send_pending(stubborn, session, now)

    await session.refresh(family)
    assert family.swept_upto == 100, "пачка разобрана поштучно — знак перешагнул"


@pytest.mark.asyncio
async def test_one_by_one_has_a_budget(session, family, morning, monkeypatch):
    """Тысяча неудаляемых не должна дать тысячу запросов в одно утро."""
    monkeypatch.setattr(sweep, "MAX_SINGLE", 5)
    monkeypatch.setattr(sweep, "SINGLE_PAUSE", 0)
    now = await morning(swept_upto=80)
    stubborn = FakeBot(
        fail_on_delete={i: _bad("message to delete not found") for i in range(10)}
    )

    await digest.send_pending(stubborn, session, now)

    assert len(stubborn.deleted_one) <= 5


# --- панели -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_marks_the_day_panel_stale_so_it_comes_back(
    session, family, bot, morning
):
    """Дыра, которую это закрывает: панель перевыпущена в 07:00.

    Тогда `panel_day` уже сегодняшний, уборка панель сотрёт, а `refresh_stale`
    сочтёт её свежей — и панели не будет весь день.
    """
    now = await morning(swept_upto=50)
    await repo.set_panel(session, family, 80, date(2026, 8, 27))

    await digest.send_pending(bot, session, now)

    await session.refresh(family)
    assert family.panel_day is None


@pytest.mark.asyncio
async def test_sweep_keeps_the_panel_message_id(session, family, bot, morning):
    """Обнулить id нельзя: `refresh_stale` пропускает семьи без панели."""
    now = await morning(swept_upto=50)
    await repo.set_panel(session, family, 80, date(2026, 8, 27))

    await digest.send_pending(bot, session, now)

    await session.refresh(family)
    assert family.panel_message_id == 80


@pytest.mark.asyncio
async def test_panel_above_the_watermark_is_left_alone(session, family, bot, morning):
    """Оборванная уборка не должна выпускать вторую панель поверх живой."""
    now = await morning(swept_upto=50)
    await repo.set_panel(session, family, 500, date(2026, 8, 27))

    await digest.send_pending(bot, session, now)

    await session.refresh(family)
    assert family.panel_day == date(2026, 8, 27)


@pytest.mark.asyncio
async def test_sweep_leaves_the_shopping_panel_id_alone(
    session, family, anya, bot, morning
):
    """Обнуление стоило бы пункта, добавленного разбором.

    `lists.refresh_panel` выходит молча при пустом `panel_message_id`: пункт лёг
    бы в базу, не показавшись в чате, до первого `/buy`.
    """
    now = await morning(swept_upto=50)
    lst = await repo.get_or_create_active_list(session, family.id)
    await repo.add_items(
        session, family_id=family.id, author_id=anya.id, list_id=lst.id,
        titles=["Молоко"],
    )
    await repo.set_list_panel(session, lst, 80)

    await digest.send_pending(bot, session, now)

    await session.refresh(lst)
    assert lst.panel_message_id == 80


# --- переезд чата -------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_forgets_the_watermark(session, family):
    """Иначе уборка молча умирает навсегда, и никто не заметит.

    У новой супергруппы id начинаются почти с нуля, а старый знак висит на
    пяти тысячах — диапазон пуст всегда.
    """
    family.swept_upto = 5000
    await session.commit()

    await repo.migrate_family_chat_id(session, family.chat_id, -100777)

    await session.refresh(family)
    assert family.swept_upto is None


# --- порядок в тикере ---------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_swept_at_dawn_comes_back_the_same_tick(
    session, family, bot, morning, monkeypatch
):
    """Сквозной: уборка сносит панель, `refresh_stale` возвращает её тут же.

    Держится это на порядке в `tick_once`: дайджест раньше панели. Переставь
    две строки — и панель, снесённая уборкой, вернётся только назавтра.
    Сессия у обеих одна, поэтому `refresh_stale` видит уже сброшенный день.
    """
    from bot.services import panel, ticker

    monkeypatch.setattr(panel.settings, "panel_debounce_seconds", 0.01)
    monkeypatch.setattr(panel, "Session", _session_factory(session))
    now = await morning(swept_upto=50)
    await repo.set_panel(session, family, 80, date(2026, 8, 27))

    await ticker.tick_once(bot, session, now)
    await _drain(panel, family.id)

    await session.refresh(family)
    # `refresh_stale` зовёт `schedule` без времени теста, поэтому день у новой
    # панели — настоящий сегодняшний. Проверяем не дату, а сам факт перевыпуска
    assert family.panel_day is not None, "панель выпущена заново"
    assert family.panel_message_id != 80
    assert family.panel_message_id > 101, "и она ниже сводки, а не выше"


def _session_factory(session):
    """Фабрика, отдающая ту же сессию: панель открывает свою, а в тесте она одна."""

    class _Keep:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda: _Keep()


async def _drain(panel, family_id):
    """Дождаться отложенной задачи панели, а не спать «на глазок»."""
    from contextlib import suppress

    task = panel._tasks.get(family_id)
    if task is not None:
        with suppress(Exception):
            await task


# --- тап по сообщению, которое уборка стёрла ----------------------------------


@pytest.mark.asyncio
async def test_tap_on_a_swept_message_does_not_kill_the_update(
    session, family, anya, bot
):
    """Новая дыра, которой до уборки не существовало.

    Человек тапает кнопку в 08:00:00: запись закрывается в базе, а перерисовка
    бьётся в уже удалённое сообщение. До правки `edit_or_ignore` пропускала
    такой отказ наружу — а упавший хендлер стоит апдейта: offset Telegram
    сдвигается независимо от исхода.
    """
    from types import SimpleNamespace

    from bot import keyboards as kb
    from bot.handlers.views import mark_done

    entry = await repo.create_entry(
        session, family_id=family.id, author_id=anya.id, kind="task", title="Дело"
    )

    async def gone(text, reply_markup=None):
        raise _bad("message to edit not found")

    call = SimpleNamespace(
        message=SimpleNamespace(
            message_id=42,
            chat=SimpleNamespace(id=family.chat_id, type="supergroup"),
            edit_text=gone,
        ),
        answer=_noop,
    )

    await mark_done(call, kb.DoneCB(entry_id=entry.id, offset=0), session, family, anya, bot)

    assert (await repo.get_entry(session, entry.id)).status == "done"


async def _noop(text: str = "", show_alert: bool = False) -> None:
    return None

