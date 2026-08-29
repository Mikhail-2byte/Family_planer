"""Голос: клиент расшифровки и кнопка-приглашение (этап 5).

Сети в тестах нет: httpx подменяется `MockTransport` (тот же приём, что в
`test_llm.py`), скачивание — `FakeBot.download`, а сама расшифровка в тестах
хендлера подменяется целиком, как подменяется `llm.ask` в `test_capture.py`.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from bot import keyboards as kb
from bot import texts
from bot.handlers import capture
from bot.handlers import voice as handler
from bot.services import http_retry, parse_log
from bot.services import timeutil as tu
from bot.services import voice as stt

STT_URL = "https://stt.test/v1/audio/transcriptions"


# --- клиент расшифровки -------------------------------------------------------


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
    monkeypatch.setattr(stt.settings, "stt_key", "gsk-test")
    monkeypatch.setattr(stt.settings, "stt_model", "whisper-test")
    monkeypatch.setattr(stt.settings, "stt_url", STT_URL)
    monkeypatch.setattr(stt.settings, "stt_proxy", "")


@pytest.fixture
def patched(monkeypatch):
    """Транспорт, отдающий заготовленные ответы; `calls` копит тела запросов."""

    def apply(responses, calls=None):
        sequence = list(responses)

        def handler_fn(request: httpx.Request) -> httpx.Response:
            if calls is not None:
                calls.append(request.content)
            item = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if isinstance(item, Exception):
                raise item
            return item

        transport = httpx.MockTransport(handler_fn)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            # MockTransport не принимает proxy, а боевой код его передаёт
            kwargs.pop("proxy", None)
            return original(*args, transport=transport, **kwargs)

        monkeypatch.setattr(stt.httpx, "AsyncClient", factory)

    return apply


def _ok(text: str) -> httpx.Response:
    return httpx.Response(200, json={"text": text})


@pytest.mark.asyncio
async def test_transcribe_returns_text(patched):
    calls: list[bytes] = []
    patched([_ok(" Купить молоко завтра ")], calls)

    assert await stt.transcribe(b"OggS") == "Купить молоко завтра"
    assert len(calls) == 1
    # Модель и язык уходят в multipart явно: автоопределение на коротких
    # записях отдаёт русскую фразу латиницей
    assert b"whisper-test" in calls[0]
    assert b"ru" in calls[0]


@pytest.mark.asyncio
async def test_busy_quota_is_retried(patched):
    calls: list[bytes] = []
    patched([httpx.Response(429), _ok("Молоко")], calls)

    assert await stt.transcribe(b"OggS") == "Молоко"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_gives_up_after_attempts(patched):
    calls: list[bytes] = []
    patched([httpx.Response(503)], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == stt.ATTEMPTS


@pytest.mark.asyncio
async def test_bad_key_is_not_retried(patched):
    """На 401 повтор даст тот же ответ — только задержит человека."""
    calls: list[bytes] = []
    patched([httpx.Response(401, text="invalid api key")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_network_failure_is_none(patched):
    calls: list[bytes] = []
    patched([httpx.ConnectError("нет сети")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == stt.ATTEMPTS


@pytest.mark.asyncio
async def test_silence_is_not_retried(patched):
    """Пустая расшифровка — это тишина в записи, повтором она не лечится."""
    calls: list[bytes] = []
    patched([_ok("   ")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_not_json_is_not_retried(patched):
    calls: list[bytes] = []
    patched([httpx.Response(200, text="<html>gateway</html>")], calls)

    assert await stt.transcribe(b"OggS") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_without_key_there_is_no_request(patched, monkeypatch):
    """Пустой STT_KEY — не ошибка: бот просто живёт без голоса."""
    calls: list[bytes] = []
    patched([_ok("Молоко")], calls)
    monkeypatch.setattr(stt.settings, "stt_key", "")

    assert await stt.transcribe(b"OggS") is None
    assert calls == []


# --- кнопка и хендлер ---------------------------------------------------------


class FakeCard:
    def __init__(self, message_id: int, chat_id: int):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")


class FakeVoiceMessage:
    """Голосовое сообщение в той части, которой пользуется хендлер."""

    def __init__(self, chat_id: int, user_id: int = 222, duration: int = 5):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.from_user = SimpleNamespace(id=user_id)
        self.message_id = 700
        self.voice = SimpleNamespace(file_id="voice-1", duration=duration)
        self.replies: list[str] = []
        self.answers: list[str] = []

    async def reply(self, text: str, **kwargs):
        self.replies.append(text)
        return FakeCard(self.message_id * 10, self.chat.id)

    async def answer(self, text: str, **kwargs):
        self.answers.append(text)
        return FakeCard(self.message_id * 10 + 1, self.chat.id)


class FakeTap:
    """Нажатие кнопки «🎤 Голосом» — обычное текстовое сообщение."""

    def __init__(self, chat_id: int, user_id: int = 222):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.from_user = SimpleNamespace(id=user_id)
        self.text = kb.BTN_VOICE
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return None

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.answers]


@pytest.fixture(autouse=True)
def _awaiting():
    """Приглашения живут в модульном словаре — между тестами он течёт."""
    handler._awaiting.clear()
    yield
    handler._awaiting.clear()


@pytest.fixture
def heard(monkeypatch):
    """Подменяет расшифровку. Возвращает список того, что ушло бы наружу."""
    calls: list[bytes] = []

    def use(result):
        async def fake_transcribe(audio, filename="voice.ogg"):
            calls.append(audio)
            return result

        monkeypatch.setattr(stt, "transcribe", fake_transcribe)
        return calls

    return use


@pytest.fixture
def parsed(monkeypatch):
    """Подменяет общий разбор: здесь проверяется путь до него, а не он сам."""
    calls: list[str] = []

    async def fake_handle(message, text, session, family, member, bot):
        calls.append(text)

    monkeypatch.setattr(capture, "handle_phrase", fake_handle)
    return calls


@pytest.mark.asyncio
async def test_voice_without_a_tap_is_ignored(family):
    """Голосовое без кнопки до хендлера не доходит — фильтр его не пускает."""
    message = FakeVoiceMessage(family.chat_id)

    assert handler._invited(message) is False


@pytest.mark.asyncio
async def test_tap_invites_and_the_next_voice_passes(family):
    tap = FakeTap(family.chat_id)
    await handler.invite(tap)

    assert texts.VOICE_ASK in tap.texts
    assert handler._invited(FakeVoiceMessage(family.chat_id)) is True


@pytest.mark.asyncio
async def test_invitation_is_personal(family):
    """Приглашение получил один человек — голосовое второго не разбирается."""
    await handler.invite(FakeTap(family.chat_id, user_id=222))

    assert handler._invited(FakeVoiceMessage(family.chat_id, user_id=333)) is False


@pytest.mark.asyncio
async def test_stale_invitation_does_not_fire(family):
    await handler.invite(FakeTap(family.chat_id))
    key = (family.chat_id, 222)
    handler._awaiting[key] = tu.now_utc() - timedelta(seconds=1)

    assert handler._invited(FakeVoiceMessage(family.chat_id)) is False


@pytest.mark.asyncio
async def test_without_key_the_button_says_so(family, monkeypatch):
    monkeypatch.setattr(handler.settings, "stt_key", "")
    tap = FakeTap(family.chat_id)

    await handler.invite(tap)

    assert texts.VOICE_OFF in tap.texts
    assert handler._awaiting == {}  # обещать «слушаю» без ключа нельзя


@pytest.mark.asyncio
async def test_recognised_text_is_shown_before_parsing(
    session, family, anya, bot, heard, monkeypatch
):
    """Сначала видно, что услышал бот, потом разбор.

    Порядок проверяется изнутри разбора, а не сравнением двух списков после:
    два независимых списка остались бы такими же, поменяй местами `reply` и
    `handle_phrase`, — то есть тест зеленел бы на сломанном коде.
    """
    heard("напомни завтра в 9 записать ребёнка к врачу")
    seen: list[list[str]] = []

    async def fake_handle(message, text, session, family, member, bot):
        seen.append(list(message.replies))  # что было сказано до разбора

    monkeypatch.setattr(capture, "handle_phrase", fake_handle)
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id)

    await handler.dictate(message, session, family, anya, bot)

    assert len(seen) == 1
    assert len(seen[0]) == 1 and "напомни завтра в 9" in seen[0][0]
    assert bot.downloaded  # запись скачали сессией бота
    assert handler._awaiting == {}  # приглашение одноразовое


@pytest.mark.asyncio
async def test_one_tap_is_one_voice_even_in_a_race(
    session, family, anya, bot, heard, parsed
):
    """Два голосовых подряд на один тап — одна расшифровка, а не две.

    aiogram обрабатывает апдейты параллельно, и оба сообщения успевают пройти
    фильтр до того, как первое доберётся до кода. Приглашение поэтому не просто
    снимается в конце, а **забирается** в начале: кто не забрал — уходит молча.
    Падает на коде, где `_awaiting.pop` стоит после расшифровки.
    """
    calls = heard("купить молоко")
    await handler.invite(FakeTap(family.chat_id))
    first = FakeVoiceMessage(family.chat_id)
    second = FakeVoiceMessage(family.chat_id)

    await asyncio.gather(
        handler.dictate(first, session, family, anya, bot),
        handler.dictate(second, session, family, anya, bot),
    )

    assert len(calls) == 1  # одна расшифровка на один тап
    assert len(parsed) == 1  # и один вызов модели
    assert len(first.replies) + len(second.replies) == 1  # одно эхо, одна карточка


@pytest.mark.asyncio
async def test_refusal_keeps_the_original_deadline(session, family, anya, bot, heard):
    """Осечка возвращает приглашение, но не продлевает окно.

    Иначе длинной записью его можно было бы держать открытым бесконечно.
    """
    heard(None)
    await handler.invite(FakeTap(family.chat_id))
    key = (family.chat_id, 222)
    before = handler._awaiting[key]

    await handler.dictate(FakeVoiceMessage(family.chat_id), session, family, anya, bot)

    assert handler._awaiting[key] == before


@pytest.mark.asyncio
async def test_too_long_is_refused_before_any_call(
    session, family, anya, bot, heard, parsed
):
    calls = heard("не должно понадобиться")
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id, duration=10_000)

    await handler.dictate(message, session, family, anya, bot)

    assert message.replies == [texts.voice_too_long(handler.settings.voice_max_seconds)]
    assert bot.downloaded == []  # длинную запись не за что качать
    assert calls == []
    assert parsed == []


@pytest.mark.asyncio
async def test_failed_transcription_keeps_the_invitation(
    session, family, anya, bot, heard, parsed
):
    """Сбой расшифровки не должен стоить второго тапа по кнопке."""
    heard(None)
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id)

    await handler.dictate(message, session, family, anya, bot)

    assert message.replies == [texts.VOICE_FAILED]
    assert parsed == []
    assert handler._invited(FakeVoiceMessage(family.chat_id)) is True


@pytest.mark.asyncio
async def test_download_failure_does_not_reach_the_endpoint(
    session, family, anya, heard, parsed
):
    from tests.conftest import FakeBot

    calls = heard("не должно понадобиться")
    bot = FakeBot(download_error=RuntimeError("Telegram недоступен"))
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id)

    await handler.dictate(message, session, family, anya, bot)

    assert message.replies == [texts.VOICE_FAILED]
    assert calls == []
    assert parsed == []


@pytest.mark.asyncio
async def test_long_speech_is_cut_in_the_echo(session, family, anya, bot, heard, parsed):
    """Длину эха задаёт говорящий — без потолка Telegram отказал бы на 4096."""
    spoken = "молоко " * 900
    heard(spoken)
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id)

    await handler.dictate(message, session, family, anya, bot)

    assert len(message.replies[0]) < texts.MESSAGE_LIMIT
    assert message.replies[0].endswith("…</i>»")
    assert parsed == [spoken]  # в разбор при этом ушла вся фраза


@pytest.mark.asyncio
async def test_echo_survives_worst_case_escaping(
    session, family, anya, bot, heard, parsed
):
    """Потолок считается до экранирования, а оно раздувает текст впятеро.

    `&` превращается в `&amp;`, и тысяча символов стала бы пятью тысячами —
    отказом Telegram, то есть потерей сообщения целиком. Резать уже готовую
    строку нельзя: обрыв внутри HTML-сущности даёт то же «can't parse entities».
    """
    heard("&" * 5000)
    await handler.invite(FakeTap(family.chat_id))
    message = FakeVoiceMessage(family.chat_id)

    await handler.dictate(message, session, family, anya, bot)

    assert len(message.replies[0]) < texts.MESSAGE_LIMIT
    assert "&amp;" in message.replies[0]  # экранирование на месте


@pytest.mark.asyncio
async def test_voice_is_logged(session, family, anya, bot, heard, parsed):
    """По `parse.log` должно быть видно и голосовые: их считают отдельно."""
    heard("купить молоко")
    await handler.invite(FakeTap(family.chat_id))

    await handler.dictate(FakeVoiceMessage(family.chat_id), session, family, anya, bot)

    lines = parse_log.PATH.read_text(encoding="utf-8").splitlines()
    assert any('"event": "voice"' in line for line in lines)


def test_voice_router_stands_before_the_wizard():
    """Иначе «🎤 Голосом», нажатое посреди `/new`, станет заголовком записи."""
    from bot.handlers import new_entry, routers, voice

    assert routers.index(voice.router) < routers.index(new_entry.router)
