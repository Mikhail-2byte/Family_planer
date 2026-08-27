"""Защитный разбор ответа модели — шаг 3a.4.

Выбранная модель заявляет только `response_format`, но не `structured_outputs`,
поэтому схему никто не гарантирует. Три случая ниже — не фантазия: на отборе
модели один ответ из восьми пришёл в ```json-обёртке.
"""

import json

import pytest

from bot.services import llm

GOOD = {"intent": "create", "items": [{"kind": "task", "title": "Купить хлеб"}]}


def test_clean_json():
    assert llm.extract_json(json.dumps(GOOD, ensure_ascii=False)) == GOOD


def test_json_in_markdown_fence():
    raw = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert llm.extract_json(raw) == GOOD


def test_fence_without_language_tag():
    raw = "```\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert llm.extract_json(raw) == GOOD


def test_json_with_chatter_around_it():
    raw = "Вот разбор:\n" + json.dumps(GOOD, ensure_ascii=False) + "\nГотово!"
    assert llm.extract_json(raw) == GOOD


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "не понял вопроса",
        "{сломанный: json,,}",
        "```json\n{нет закрывающей\n```",
    ],
)
def test_garbage_gives_none(raw):
    assert llm.extract_json(raw) is None


def test_json_array_is_not_accepted():
    """Схема обещает объект. Массив — это уже не она, и притворяться нечем."""
    assert llm.extract_json('[{"kind": "task"}]') is None


def test_cyrillic_survives():
    raw = '{"title": "Позвонить маме", "body": "в 19:00"}'
    assert llm.extract_json(raw)["title"] == "Позвонить маме"
