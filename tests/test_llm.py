"""Клиент OpenRouter — шаг 3a.2.

Сети в тестах нет: httpx подменяется `MockTransport`, а сон между ретраями —
monkeypatch'ем, иначе тест ждал бы шесть секунд.
"""

import json

import httpx
import pytest

from bot.services import http_retry, llm


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Паузы между ретраями живут в `http_retry` — там их и гасим.

    Раньше подменялся `asyncio.sleep` в самом модуле клиента; с выносом общей
    политики повторов спать стало некому, и тест падал на отсутствии атрибута.
    """

    async def instant(_seconds):
        return None

    monkeypatch.setattr(http_retry.asyncio, "sleep", instant)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(llm.settings, "openrouter_key", "sk-test")
    monkeypatch.setattr(llm.settings, "openrouter_model", "test/model")
    monkeypatch.setattr(llm.settings, "openrouter_proxy", "")


def _client_returning(responses, calls=None):
    """Подменить httpx.AsyncClient транспортом, отдающим `responses` по очереди."""
    sequence = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        item = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


@pytest.fixture
def patched(monkeypatch):
    def apply(responses, calls=None):
        transport = _client_returning(responses, calls)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs.pop("proxy", None)
            return original(*args, transport=transport, **kwargs)

        monkeypatch.setattr(llm.httpx, "AsyncClient", factory)

    return apply


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_returns_parsed_json(patched):
    patched([_ok('{"intent": "create", "items": []}')])
    assert await llm.ask("system", "купить хлеб") == {"intent": "create", "items": []}


@pytest.mark.asyncio
async def test_request_denies_data_collection(patched):
    """Приватность не должна зависеть только от галки в аккаунте (шаг 3a.0)."""
    calls = []
    patched([_ok("{}")], calls)
    await llm.ask("system", "текст")
    assert calls[0]["provider"] == {"data_collection": "deny"}
    assert calls[0]["model"] == "test/model"


@pytest.mark.asyncio
async def test_network_failure_returns_none_not_exception(patched):
    """Главное требование шага: сетевая ошибка не выходит наружу."""
    patched([httpx.ConnectError("сеть отвалилась")])
    assert await llm.ask("system", "текст") is None


@pytest.mark.asyncio
async def test_retries_then_succeeds(patched):
    calls = []
    patched([httpx.Response(429, json={}), httpx.Response(503, json={}), _ok('{"ok": 1}')], calls)
    assert await llm.ask("system", "текст") == {"ok": 1}
    assert len(calls) == 3, "две неудачи должны были дать два ретрая"


@pytest.mark.asyncio
async def test_gives_up_after_three_attempts(patched):
    calls = []
    patched([httpx.Response(429, json={})], calls)
    assert await llm.ask("system", "текст") is None
    assert len(calls) == llm.ATTEMPTS


@pytest.mark.asyncio
async def test_bad_key_is_not_retried(patched):
    """401 повторять бессмысленно — ключ от повтора не починится."""
    calls = []
    patched([httpx.Response(401, json={"error": {"message": "no auth"}})], calls)
    assert await llm.ask("system", "текст") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_error_inside_200_is_retried(patched):
    """Отказ провайдера приезжает и с кодом 200 — с полем error в теле."""
    calls = []
    patched(
        [
            httpx.Response(200, json={"error": {"message": "rate-limited upstream"}}),
            _ok('{"ok": 1}'),
        ],
        calls,
    )
    assert await llm.ask("system", "текст") == {"ok": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_empty_content_is_retried(patched):
    """Рассуждающая модель умеет оборваться, оставив content пустым."""
    calls = []
    patched(
        [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "error"}
                    ]
                },
            ),
            _ok('{"ok": 1}'),
        ],
        calls,
    )
    assert await llm.ask("system", "текст") == {"ok": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_markdown_wrapped_answer_is_understood(patched):
    patched([_ok('```json\n{"intent": "create"}\n```')])
    assert await llm.ask("system", "текст") == {"intent": "create"}


@pytest.mark.asyncio
async def test_no_key_means_no_call(patched, monkeypatch):
    """Без ключа бот обязан работать — просто без разбора текста."""
    monkeypatch.setattr(llm.settings, "openrouter_key", "")
    calls = []
    patched([_ok("{}")], calls)
    assert await llm.ask("system", "текст") is None
    assert calls == []


@pytest.mark.asyncio
async def test_no_model_means_no_call(patched, monkeypatch):
    monkeypatch.setattr(llm.settings, "openrouter_model", "")
    calls = []
    patched([_ok("{}")], calls)
    assert await llm.ask("system", "текст") is None
    assert calls == []
