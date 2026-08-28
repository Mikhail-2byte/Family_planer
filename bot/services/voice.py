"""Расшифровка голосовых сообщений (этап 5).

Транспорт и только транспорт: голос → текст. Что с этим текстом делать, знает
`handlers/voice.py`, а разбирает его тот же `capture.handle_phrase`, что и
обычную фразу.

Наружу исключений не выпускает — при любом сбое возвращает `None`, как и
`services/llm.py`. Бот обязан работать без расшифровки: пустой `STT_KEY`
просто выключает кнопку «🎤 Голосом», остальное живёт как раньше.

Эндпоинт — Whisper-совместимый (по умолчанию Groq), а не мультимодальная
модель OpenRouter, и это даёт две вещи. Первая: `ogg/opus` Telegram
принимается как есть, поэтому `ffmpeg` проекту не нужен вовсе — а `PLAN.md`
закладывал его отдельным шагом. Вторая: своя суточная квота, не пересекающаяся
с лимитом OpenRouter, на котором держится текстовый разбор через «+».
"""

import asyncio
import logging

import httpx

from bot.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 60.0
ATTEMPTS = 3  # первая попытка плюс два ретрая
BACKOFF = 2.0  # секунды перед вторым ретраем, дальше вдвое

# Те же коды, что у `llm.py`, и по той же причине: 429 — занятая квота,
# 5xx — сбой на стороне провайдера. На 401/402/404 повтор даст то же самое
RETRY_CODES = frozenset({408, 429, 500, 502, 503, 504})


async def transcribe(audio: bytes, filename: str = "voice.ogg") -> str | None:
    """Текст записи или `None` при любом сбое."""
    if not settings.stt_key:
        return None  # без ключа голос выключен, и это не ошибка
    if not settings.stt_model:
        log.warning("STT_MODEL пуст — расшифровка выключена")
        return None

    # Клиент на вызов, а не на модуль: голосовых единицы в день, зато не нужно
    # закрывать живое соединение на остановке бота (то же решение, что в llm.py)
    async with httpx.AsyncClient(proxy=settings.stt_proxy or None) as client:
        for attempt in range(ATTEMPTS):
            outcome = await _try_once(client, audio, filename)
            if outcome is not None:
                return outcome or None  # пустая строка — тоже неудача
            if attempt < ATTEMPTS - 1:
                await asyncio.sleep(BACKOFF * 2**attempt)
    return None


async def _try_once(
    client: httpx.AsyncClient, audio: bytes, filename: str
) -> str | None:
    """Текст расшифровки, либо `None` — «имеет смысл повторить»."""
    try:
        response = await client.post(
            settings.stt_url,
            headers={"Authorization": f"Bearer {settings.stt_key}"},
            files={"file": (filename, audio, "audio/ogg")},
            data={
                "model": settings.stt_model,
                # Язык задан явно: на коротких записях автоопределение
                # ошибается и отдаёт русскую фразу латиницей
                "language": "ru",
                "response_format": "json",
                "temperature": "0",
            },
            timeout=TIMEOUT,
        )
    except Exception:
        log.warning("Эндпоинт расшифровки недоступен", exc_info=True)
        return None

    if response.status_code in RETRY_CODES:
        log.warning("Расшифровка ответила %s — повторим", response.status_code)
        return None
    if response.status_code != 200:
        log.error(
            "Расшифровка отказала: %s %.200s", response.status_code, response.text
        )
        return ""  # повтор бессмыслен: плохой ключ, нет денег, нет модели

    try:
        body = response.json()
    except ValueError:
        log.error("Расшифровка вернула не JSON: %.200s", response.text)
        return ""

    # Проверки `body["error"]` при HTTP 200, как у `llm._try_once`, здесь нет
    # намеренно: то повадка OpenRouter, увиденная живьём на отборе модели, а не
    # общее правило. Ответ без поля `text` и так уходит в отказ строкой ниже

    text = (body.get("text") or "").strip()
    if not text:
        log.warning("Расшифровка вернула пустой текст: %.200s", str(body))
        return ""  # тишина в записи повтором не лечится
    return text
