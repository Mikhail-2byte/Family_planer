"""Общая политика повторов: ретраи и потолок ожидания.

Раньше цикл повторов существовал в двух экземплярах — в `llm.py` и `voice.py` —
и совпадал построчно. Разошлись бы они молча, а второй экземпляр обслуживает
голос: путь, где два таких вызова идут подряд и ожидание складывается.

Главное здесь — `TOTAL_BUDGET`. До него верхняя граница была `ATTEMPTS ×
TIMEOUT` плюс паузы, то есть 186 секунд на вызов и до шести минут на голосовое.
Столько человек в чате не ждёт: он читает это как «бот умер».
"""

import asyncio

import pytest

from bot.services import http_retry


@pytest.mark.asyncio
async def test_first_success_stops_the_loop():
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        return "готово"

    assert await http_retry.with_retries(attempt, what="тест") == "готово"
    assert calls == 1


@pytest.mark.asyncio
async def test_retries_until_success(monkeypatch):
    monkeypatch.setattr(http_retry.asyncio, "sleep", _instant)
    outcomes = [None, None, "готово"]

    async def attempt():
        return outcomes.pop(0)

    assert await http_retry.with_retries(attempt, what="тест") == "готово"
    assert outcomes == []


@pytest.mark.asyncio
async def test_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(http_retry.asyncio, "sleep", _instant)
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        return None

    assert await http_retry.with_retries(attempt, what="тест") is None
    assert calls == http_retry.ATTEMPTS


@pytest.mark.asyncio
async def test_empty_result_is_not_retried(monkeypatch):
    """Пустая строка — «сдаёмся сразу», её отличает вызывающий, а не цикл.

    Контракт достался от `_try_once` в обоих клиентах и менять его нельзя:
    для `llm` пустой ответ означает плохой ключ, а повтор даст то же самое.
    """
    monkeypatch.setattr(http_retry.asyncio, "sleep", _instant)
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        return ""

    assert await http_retry.with_retries(attempt, what="тест") == ""
    assert calls == 1


@pytest.mark.asyncio
async def test_budget_cuts_a_hanging_attempt():
    """Потолок обязан накрывать саму попытку, а не только паузы между ними.

    Тест, ради которого модуль и заведён. Проверка «успеем ли поспать» его бы
    не прошла: висящая попытка до пауз просто не доходит.
    """
    started = 0

    async def attempt():
        nonlocal started
        started += 1
        await asyncio.sleep(60)  # провайдер молчит
        return "поздно"

    result = await http_retry.with_retries(attempt, what="тест", budget=0.05)

    assert result is None
    assert started == 1, "вторая попытка при исчерпанном бюджете не нужна"


@pytest.mark.asyncio
async def test_budget_is_shorter_than_the_arithmetic_worst_case():
    """Смысл потолка — быть **меньше** `ATTEMPTS × TIMEOUT`, иначе он декоративен."""
    worst = http_retry.ATTEMPTS * http_retry.TIMEOUT
    assert worst > http_retry.TOTAL_BUDGET


async def _instant(_seconds):
    return None
