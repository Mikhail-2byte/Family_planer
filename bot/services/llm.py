"""Клиент OpenRouter и защитный разбор его ответа (этап 3a).

Наружу не выпускает исключений: при любом сбое — сети, отказе провайдера,
пустом ключе — возвращает `None`. Бот обязан работать без LLM, а запасной
путь на `dateparser` подключается на шаге 3b.1.

Почему разбор ответа отдельно и с оговорками: выбранная модель
`minimax/minimax-m3:free` заявляет только `response_format`, но не
`structured_outputs` — строгой гарантии схемы от провайдера нет. На отборе
модели один ответ из восьми пришёл завёрнутым в ```json-блок, так что
markdown-обёртка — не теоретический случай.
"""

import json
import logging
import re

import httpx

from bot.config import settings
from bot.services import http_retry

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/chat/completions"

# Политика повторов общая с расшифровкой голоса — см. `http_retry`. Раньше цикл
# и константы existed здесь и там построчно одинаковыми, и разошлись бы молча.
# Имена оставлены модулю: на них смотрят тесты и читатель, пришедший сюда
TIMEOUT = http_retry.TIMEOUT
ATTEMPTS = http_retry.ATTEMPTS
BACKOFF = http_retry.BACKOFF
RETRY_CODES = http_retry.RETRY_CODES

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def ask(system: str, user: str, *, model: str | None = None) -> dict | None:
    """Спросить модель и вернуть разобранный JSON. Ошибок наружу не выпускает."""
    raw = await complete(system, user, model=model)
    if raw is None:
        return None
    parsed = extract_json(raw)
    if parsed is None:
        log.warning("Модель вернула не JSON: %.200s", raw)
    return parsed


async def complete(system: str, user: str, *, model: str | None = None) -> str | None:
    """Сырой текст ответа модели или `None`."""
    if not settings.openrouter_key:
        return None  # без ключа бот живёт, просто без разбора текста
    chosen = model or settings.openrouter_model
    if not chosen:
        log.warning("OPENROUTER_MODEL пуст — разбор текста выключен")
        return None

    payload = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        # Дублирует настройку аккаунта (шаг 3a.0): приватность не должна
        # зависеть от галки, которую можно вернуть или потерять со сменой ключа
        "provider": {"data_collection": "deny"},
    }

    # Клиент на вызов, а не на модуль: запросов единицы в день, а живой
    # AsyncClient пришлось бы закрывать на остановке бота
    async with httpx.AsyncClient(proxy=settings.openrouter_proxy or None) as client:
        outcome = await http_retry.with_retries(
            lambda: _try_once(client, payload), what="OpenRouter"
        )
    return outcome or None  # пустая строка — тоже неудача


async def _try_once(client: httpx.AsyncClient, payload: dict) -> str | None:
    """Текст ответа, либо `None` — «имеет смысл повторить»."""
    try:
        response = await client.post(
            URL,
            headers={"Authorization": f"Bearer {settings.openrouter_key}"},
            json=payload,
            timeout=TIMEOUT,
        )
    except Exception:
        log.warning("OpenRouter недоступен", exc_info=True)
        return None

    if response.status_code in RETRY_CODES:
        log.warning("OpenRouter ответил %s — повторим", response.status_code)
        return None
    if response.status_code != 200:
        log.error("OpenRouter отказал: %s %.200s", response.status_code, response.text)
        return ""  # повтор бессмыслен: плохой ключ, нет денег, нет модели

    try:
        body = response.json()
    except ValueError:
        log.error("OpenRouter вернул не JSON: %.200s", response.text)
        return ""

    # Ошибка провайдера приезжает и с кодом 200 — с полем error в теле
    if body.get("error"):
        log.warning("Провайдер отказал: %.200s", str(body["error"]))
        return None

    choices = body.get("choices") or []
    if not choices:
        log.error("OpenRouter вернул ответ без choices: %.200s", str(body))
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        # Рассуждающие модели умеют обрываться, оставив content пустым
        log.warning("Пустой ответ, finish_reason=%s", choices[0].get("finish_reason"))
        return None
    return content


def extract_json(raw: str) -> dict | None:
    """Достать объект JSON из ответа модели. Мусор — `None`, не исключение.

    Три случая с отбора модели: чистый JSON; JSON внутри ```json-обёртки;
    текст, в котором JSON — только часть.
    """
    if not raw:
        return None
    text = raw.strip()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        value = json.loads(text)
    except ValueError:
        pass
    else:
        # Разобралось — вердикт окончательный. Если это не объект (например
        # голый массив items), вырезать из него внутренний `{...}` нельзя:
        # получился бы первый элемент, выданный за весь ответ. Честнее отказать
        # и уйти на запасной разбор, чем тихо подменить смысл
        return value if isinstance(value, dict) else None

    # Не разобралось целиком — последняя попытка: от первой скобки до
    # последней. Помогает, когда модель приписала «Вот разбор:» или хвост
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None
