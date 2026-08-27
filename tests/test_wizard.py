"""Ручной ввод даты в мастере /new — этап 1.10."""

from datetime import date

import pytest

from bot.handlers.new_entry import _parse_day

TODAY = date(2026, 8, 27)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("28.08", date(2026, 8, 28)),
        ("28.08.2026", date(2026, 8, 28)),
        ("28.08.26", date(2026, 8, 28)),
        ("28/08", date(2026, 8, 28)),
        (" 1.9 ", date(2026, 9, 1)),
        # Без года прошедшая дата означает следующий год, а не «вчера»
        ("01.03", date(2027, 3, 1)),
        # С явным годом прошлое остаётся прошлым — человек так и написал
        ("01.03.2026", date(2026, 3, 1)),
    ],
)
def test_parse_day(raw, expected):
    assert _parse_day(raw, TODAY) == expected


@pytest.mark.parametrize("raw", ["завтра", "32.01", "", "8", "13.13"])
def test_parse_day_rejects_garbage(raw):
    assert _parse_day(raw, TODAY) is None
