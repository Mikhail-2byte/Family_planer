"""Фильтр обращения к боту — шаг 3a.5.

Главное требование этапа: обычная переписка семьи **не даёт ни одного вызова
LLM**. Это не про экономию — это про то, что чужие разговоры не должны уезжать
в сторонний API.
"""

from types import SimpleNamespace

import pytest

from bot.filters import IsTrigger

BOT_ID = 777
BOT_NAME = "Family_vlasov_planner_bot"


class FakeBot:
    """`bot.me()` — единственное, что фильтру нужно от бота."""

    async def me(self):
        return SimpleNamespace(id=BOT_ID, username=BOT_NAME)


def _message(text, reply_from_id=None):
    reply = None
    if reply_from_id is not None:
        reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_from_id))
    return SimpleNamespace(text=text, reply_to_message=reply)


async def _check(text, reply_from_id=None):
    return await IsTrigger()(_message(text, reply_from_id), FakeBot())


# --- что считается обращением -------------------------------------------------


@pytest.mark.asyncio
async def test_plus_prefix_triggers():
    assert await _check("+купить молоко завтра к 19") == {"payload": "купить молоко завтра к 19"}


@pytest.mark.asyncio
async def test_reply_to_bot_triggers():
    assert await _check("завтра в 19", reply_from_id=BOT_ID) == {"payload": "завтра в 19"}


@pytest.mark.asyncio
async def test_mention_triggers_and_is_cut_out():
    got = await _check(f"@{BOT_NAME} купи хлеб")
    assert got == {"payload": "купи хлеб"}, "упоминание не должно попасть в разбор"


@pytest.mark.asyncio
async def test_mention_in_the_middle_triggers():
    assert await _check(f"купи хлеб @{BOT_NAME} пожалуйста") == {"payload": "купи хлеб пожалуйста"}


@pytest.mark.asyncio
async def test_mention_is_case_insensitive():
    """Telegram хранит регистр имени, а человек пишет как придётся."""
    assert await _check(f"@{BOT_NAME.lower()} купи хлеб") == {"payload": "купи хлеб"}


# --- что обращением НЕ считается ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "а что там с отпуском",
        "ага",
        "завтра в 19:00 встречаемся",
        "напиши плюс в конце+",
        "@другой_бот купи хлеб",
    ],
)
async def test_ordinary_talk_is_ignored(text):
    assert await _check(text) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "/today",
        "/tday@Family_vlasov_planner_bot",  # опечатка в команде + упоминание
        "/start",
    ],
)
async def test_commands_are_not_addressed_to_the_model(text):
    """Команды разбирают свои роутеры; до фильтра доходят только несуществующие.

    Без этой ветки опечатка в команде считалась бы обращением по упоминанию и
    стоила бы вызова модели.
    """
    assert await _check(text) is False


@pytest.mark.asyncio
async def test_command_in_reply_to_bot_is_still_not_a_trigger():
    assert await _check("/today", reply_from_id=777) is False


@pytest.mark.asyncio
async def test_reply_to_a_human_is_ignored():
    assert await _check("да, давай", reply_from_id=42) is False


@pytest.mark.asyncio
async def test_reply_to_another_bot_is_ignored():
    """Сравнение идёт по id, а не по признаку «это бот»."""
    assert await _check("текст", reply_from_id=999) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "+", "+   ", f"@{BOT_NAME}", f"@{BOT_NAME}   "])
async def test_empty_address_gives_nothing_to_parse(text):
    assert await _check(text) is False


@pytest.mark.asyncio
async def test_photo_without_text_is_ignored():
    """У сообщения с картинкой `text` пуст — падать на None нельзя."""
    assert await _check(None) is False
