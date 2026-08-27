"""Конвертация и форматирование времени — этап 1.2.

Europe/Moscow переход на летнее время не имеет, поэтому DST проверяем
на Europe/Berlin: если код сломается на переводе часов, здесь это видно.
"""

from datetime import date, datetime, timedelta

import pytest

from bot.services import timeutil as tu

MSK = "Europe/Moscow"
BERLIN = "Europe/Berlin"


def test_roundtrip_msk():
    local = datetime(2026, 8, 27, 19, 0)
    assert tu.to_utc(local, MSK) == datetime(2026, 8, 27, 16, 0)
    assert tu.to_local(datetime(2026, 8, 27, 16, 0), MSK) == local


def test_roundtrip_is_stable_across_the_year():
    for month in range(1, 13):
        local = datetime(2026, month, 15, 12, 30)
        assert tu.to_local(tu.to_utc(local, BERLIN), BERLIN) == local


def test_dst_shift_changes_the_offset():
    # Берлин переходит на летнее время в ночь на 29 марта 2026
    winter = tu.to_utc(datetime(2026, 3, 28, 12, 0), BERLIN)
    summer = tu.to_utc(datetime(2026, 3, 30, 12, 0), BERLIN)
    assert winter.hour == 11  # UTC+1
    assert summer.hour == 10  # UTC+2


def test_day_bounds_cover_exactly_24h_in_msk():
    start, end = tu.day_bounds(date(2026, 8, 27), MSK)
    assert start == datetime(2026, 8, 26, 21, 0)
    assert end == datetime(2026, 8, 27, 21, 0)
    assert end - start == timedelta(hours=24)


def test_day_bounds_shrink_on_the_dst_day():
    """В день перехода на летнее время локальные сутки короче на час."""
    start, end = tu.day_bounds(date(2026, 3, 29), BERLIN)
    assert end - start == timedelta(hours=23)


def test_week_bounds_start_on_monday():
    start, end = tu.week_bounds(date(2026, 8, 27), MSK)  # четверг
    assert tu.to_local(start, MSK) == datetime(2026, 8, 24, 0, 0)  # понедельник
    assert tu.to_local(end, MSK) == datetime(2026, 8, 31, 0, 0)
    assert end - start == timedelta(days=7)


def test_local_today_near_midnight():
    """23:30 по Москве — это UTC 20:30 того же дня, но дата локальная."""
    assert tu.local_today(MSK, now=datetime(2026, 8, 27, 20, 30)) == date(2026, 8, 27)
    # 00:30 по Москве 28-го = 21:30 UTC 27-го
    assert tu.local_today(MSK, now=datetime(2026, 8, 27, 21, 30)) == date(2026, 8, 28)


def test_parse_hhmm():
    assert tu.parse_hhmm("08:00").hour == 8
    assert tu.parse_hhmm(" 19:45 ").minute == 45
    with pytest.raises(ValueError):
        tu.parse_hhmm("восемь")


NOW = datetime(2026, 8, 27, 9, 0)  # UTC → 12:00 по Москве, четверг


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2026, 8, 27, 19, 0), "сегодня, 19:00"),
        (datetime(2026, 8, 28, 19, 0), "завтра, 28 авг, 19:00"),
        (datetime(2026, 8, 29, 19, 0), "послезавтра, 29 авг, 19:00"),
        (datetime(2026, 8, 26, 19, 0), "вчера, 26 авг, 19:00"),
        (datetime(2026, 8, 31, 19, 0), "пн, 31 авг, 19:00"),
        (datetime(2026, 12, 31, 19, 0), "31 дек, 19:00"),
        (datetime(2027, 3, 3, 19, 0), "3 мар 2027, 19:00"),
    ],
)
def test_fmt_due(local, expected):
    assert tu.fmt_due(tu.to_utc(local, MSK), MSK, now=NOW) == expected


def test_fmt_due_all_day_hides_time():
    utc = tu.to_utc(datetime(2026, 8, 28, 0, 0), MSK)
    assert tu.fmt_due(utc, MSK, all_day=True, now=NOW) == "завтра, 28 авг"


@pytest.mark.parametrize(
    "local, expected",
    [
        (datetime(2026, 8, 27, 8, 5), "сегодня в 08:05"),
        (datetime(2026, 8, 26, 21, 14), "вчера в 21:14"),
        (datetime(2026, 8, 20, 21, 14), "20 авг в 21:14"),
    ],
)
def test_fmt_when(local, expected):
    assert tu.fmt_when(tu.to_utc(local, MSK), MSK, now=NOW) == expected
