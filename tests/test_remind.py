"""Разбор дат без LLM и команда /remind — этап 2.5.

Половина тестов здесь — регрессии на молчаливое враньё `dateparser`: он
возвращает неверную дату, а не отказ, и без этих проверок бот будет
выстреливать напоминаниями не вовремя, а заметит это только человек.
"""

from datetime import datetime

import pytest

from bot.db import repo
from bot.services import nlp_fallback as nlp
from bot.services import timeutil as tu

MSK = "Europe/Moscow"
NOW_LOCAL = datetime(2026, 8, 27, 12, 0)  # четверг


# --- что должно разбираться --------------------------------------------------


@pytest.mark.parametrize(
    "raw, when, text",
    [
        ("через 2 минуты позвонить маме", datetime(2026, 8, 27, 12, 2), "позвонить маме"),
        ("позвонить маме через 2 минуты", datetime(2026, 8, 27, 12, 2), "позвонить маме"),
        ("через час выключить духовку", datetime(2026, 8, 27, 13, 0), "выключить духовку"),
        ("завтра в 19:30 забрать посылку", datetime(2026, 8, 28, 19, 30), "забрать посылку"),
        # Голый час: без нормализации dateparser терял бы 19 и брал полдень
        ("завтра в 19 забрать посылку", datetime(2026, 8, 28, 19, 0), "забрать посылку"),
        ("выпить таблетки завтра в 9", datetime(2026, 8, 28, 9, 0), "выпить таблетки"),
        # «9» тут — час, а не девятое число месяца
        ("в понедельник в 9 к врачу", datetime(2026, 8, 31, 9, 0), "к врачу"),
        # «10» тут — час, а не 2110 год
        ("1 сентября в 10 линейка", datetime(2026, 9, 1, 10, 0), "линейка"),
        # Части суток dateparser не понимает вовсе — переводим их сами
        ("в 7 вечера ужин", datetime(2026, 8, 27, 19, 0), "ужин"),
        # 9 утра сегодня уже прошло, поэтому «завтра» — это верный ответ,
        # а не сдвиг: напоминание на прошедшее время бессмысленно
        ("в 9 утра зарядка", datetime(2026, 8, 28, 9, 0), "зарядка"),
    ],
)
def test_parse_when_understands(raw, when, text):
    parsed = nlp.parse_when(raw, NOW_LOCAL)
    assert parsed is not None, "фраза должна разбираться"
    assert parsed.when == when
    assert parsed.text == text


# --- что должно честно отказывать -------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # dateparser вырезает огрызок «03 в 8» и уезжает в 2027 год.
        # Лучше отказ, чем напоминание на неверную дату.
        "12.03 в 8 сдать анализы",
        "сдать анализы 12.03 в 8",
        "купить молоко",
        "",
        "   ",
    ],
)
def test_parse_when_refuses_rather_than_guesses(raw):
    assert nlp.parse_when(raw, NOW_LOCAL) is None


@pytest.mark.parametrize(
    "raw",
    [
        "каждый вторник в 19 тренировка",
        "каждую пятницу платить за садик",
        "ежедневно пить таблетки",
        "по будням будильник",
        # Порядок слов в /remind свободный — ключевое слово бывает и в хвосте.
        # С якорем на начале строки эти четыре молча становились разовыми:
        # «позвонить маме каждый» на ближайший вторник, и никто бы не заметил
        "позвонить маме каждый вторник в 19:00",
        "тренировка каждую пятницу",
        "выносить мусор ежедневно в 21:00",
        "будильник по будням",
    ],
)
def test_recurring_phrases_are_recognised(raw):
    """Иначе «каждый вторник» молча станет разовым, потеряв слово «вторник»."""
    assert nlp.looks_recurring(raw) is True


def test_plain_phrases_are_not_recurring():
    assert nlp.looks_recurring("через 2 минуты позвонить маме") is False
    assert nlp.looks_recurring("завтра в 19 забрать посылку") is False


def test_normalize_leaves_explicit_time_alone():
    assert nlp.normalize("завтра в 19:30") == "завтра в 19:30"
    assert nlp.normalize("12.03 в 8") == "12.03 в 8:00"


# --- команда -----------------------------------------------------------------


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class FakeCommand:
    def __init__(self, args):
        self.args = args


async def _run(session, family, anya, args):
    from bot.handlers.remind import cmd_remind

    message = FakeMessage()
    await cmd_remind(message, FakeCommand(args), session, family, anya)
    return message.replies


@pytest.mark.asyncio
async def test_remind_creates_reminder_with_utc_fire_at(session, family, anya):
    replies = await _run(session, family, anya, "завтра в 19:00 забрать посылку")

    due_far_ahead = await repo.due_reminders(
        session, tu.to_utc(datetime(2026, 12, 31), MSK)
    )
    assert len(due_far_ahead) == 1
    reminder = due_far_ahead[0]
    assert reminder.text == "забрать посылку"
    # В базе — UTC, у Москвы это 16:00
    assert tu.to_local(reminder.fire_at, MSK).hour == 19
    assert "забрать посылку" in replies[0]


@pytest.mark.asyncio
async def test_remind_without_args_shows_usage(session, family, anya):
    from bot import texts

    assert await _run(session, family, anya, "") == [texts.REMIND_USAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    ["каждый вторник в 19 тренировка", "тренировка каждый вторник в 19"],
)
async def test_remind_refuses_recurring(session, family, anya, raw):
    from bot import texts

    replies = await _run(session, family, anya, raw)
    assert replies == [texts.REMIND_RECURRING]
    assert await repo.due_reminders(session, tu.to_utc(datetime(2027, 1, 1), MSK)) == []


@pytest.mark.asyncio
async def test_remind_refuses_unparsable(session, family, anya):
    from bot import texts

    replies = await _run(session, family, anya, "купить молоко")
    assert replies == [texts.REMIND_NO_DATE]
    assert await repo.due_reminders(session, tu.to_utc(datetime(2027, 1, 1), MSK)) == []


@pytest.mark.asyncio
async def test_remind_rejects_past(session, family, anya):
    """Иначе тикер отработает догонкой и выстрелит немедленно."""
    replies = await _run(session, family, anya, "1 января в 10 поздравить")
    # Прошлое либо отвергнуто, либо разобрано как будущий год — но записи
    # на прошедшее время быть не должно
    due_now = await repo.due_reminders(session, tu.now_utc())
    assert due_now == []
    assert replies


@pytest.mark.asyncio
async def test_remind_escapes_html_in_reply(session, family, anya):
    replies = await _run(session, family, anya, "через час <b>жирное</b> дело")
    assert "&lt;b&gt;жирное&lt;/b&gt;" in replies[0]
