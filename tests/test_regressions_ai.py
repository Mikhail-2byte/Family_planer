"""Отказ ИИ виден и переживаем (этап 12, инцидент 01.09.2026).

Четвёртый файл регрессий, и повод у него свой. `test_regressions.py` — ревизия
этапов 0–1, `test_regressions_audit.py` — сплошная ревизия 0–7 по коду,
`test_regressions_live.py` — претензии владельца после живого прогона этапа 10.
Здесь другое: один вечер, одна фраза и один отказ провайдера.

Что случилось. Голосовое разобралось без ИИ, и понять, сломан ИИ или нет, было
неоткуда. В `bot.log` лежало::

    20:14:30 ERROR bot.services.llm: OpenRouter отказал: 401 {"error":
      {"message":"Provider returned error","code":401,"metadata":
      {"raw":"Invalid API key: HTTP 503","provider_name":"GMICloud",
       "is_byok":false}}}

Ключ владельца был исправен: `is_byok: false` означает ключ самого OpenRouter к
провайдеру, а не наш. У модели `minimax/minimax-m3:free` провайдер ровно один —
GMICloud, — и его полминуты хватило, чтобы разбор упал целиком, а карточка
сказала то же самое «Разобрал без ИИ», что говорит при пустом ключе и при
исчерпанном лимите.

Правило то же, что у соседей: **каждый тест обязан падать на коде «до правки»**.
Проверено прогоном на `git worktree` с кодом этапа 11 — 6 из 6 красные. С
оговоркой, которой у соседних файлов не было: правка меняет сам контракт
`llm.ask`, поэтому на старом коде тесты падают на отсутствии `llm.Answer`, а не
на самом дефекте. Чтобы «красный» не оказался бухгалтерским, каждый дефект был
показан на том же дереве отдельно, вручную:

- кривой `OPENROUTER_PROXY` — `llm.ask` выпускал наружу
  `ValueError: Unknown scheme for proxy URL`;
- `texts.capture_card` параметра `reason` не имела вовсе, и карточка при пустом
  ключе и при 401 от провайдера совпадала дословно;
- `calls_today()` до и после неудачного вызова давал ноль, хотя к провайдеру
  ушли три запроса.

Последний тест в файле — обратная половина: он зелёный по обе стороны и
краснеет, если правку сделать слишком широкой.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot import texts
from bot.handlers import capture
from bot.services import llm, parse_log, parsing


class FakeCard:
    def __init__(self, message_id: int, chat_id: int):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")

    async def edit_text(self, text: str, **kwargs) -> None:
        return None


class FakeMessage:
    def __init__(self, chat_id: int = -1001, message_id: int = 500):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.message_id = message_id
        self.replies: list[str] = []
        self._next_id = message_id

    async def answer(self, text: str, **kwargs):
        self.replies.append(text)
        self._next_id += 1
        return FakeCard(self._next_id, self.chat.id)

    @property
    def texts(self) -> list[str]:
        return list(self.replies)


@pytest_asyncio.fixture
async def phrase(session, family, anya, bot, monkeypatch):
    """Прогон `handle_phrase` с заданным ответом модели."""

    async def run(answer, text="купить молоко завтра"):
        async def fake_ask(system, user, **kwargs):
            return answer

        monkeypatch.setattr(llm, "ask", fake_ask)
        message = FakeMessage(chat_id=family.chat_id)
        await capture.handle_phrase(message, text, session, family, anya, bot)
        return message

    return run


def _log_lines() -> list[str]:
    if not parse_log.PATH.exists():
        return []
    return parse_log.PATH.read_text(encoding="utf-8").splitlines()


def test_the_card_names_the_reason():
    """Двенадцать разных бед показывались одной строкой — это и была беда.

    До этапа 12 `capture_card` не знала слова «причина» вовсе: и пустой ключ, и
    401 от провайдера давали дословно одинаковую карточку.
    """
    item = parsing.Item(kind="task", title="купить молоко")

    no_key = texts.capture_card([item], "Europe/Moscow", via="dateparser", reason=llm.NO_KEY)
    down = texts.capture_card(
        [item], "Europe/Moscow", via="dateparser", reason=llm.UNAVAILABLE
    )

    assert no_key != down
    assert texts.ai_reason(llm.NO_KEY) in no_key
    assert texts.ai_reason(llm.UNAVAILABLE) in down


@pytest.mark.asyncio
async def test_a_broken_proxy_does_not_escape_the_client(monkeypatch):
    """Конструктор `AsyncClient` стоял вне `try` — и ронял хендлер до карточки.

    Упавший апдейт теряется навсегда: offset Telegram сдвигается независимо от
    исхода. То есть человек не получал ни разбора, ни запасного, ни слова.
    """
    monkeypatch.setattr(llm.settings, "openrouter_key", "sk-test")
    monkeypatch.setattr(llm.settings, "llm_models", "openrouter:main/model")

    def broken(*args, **kwargs):
        raise ValueError("Unknown scheme for proxy URL")

    monkeypatch.setattr(llm.httpx, "AsyncClient", broken)

    answer = await llm.ask("system", "текст")

    assert answer.data is None
    assert answer.reason == llm.UNAVAILABLE


@pytest.mark.asyncio
async def test_a_failed_call_grows_the_daily_counter(phrase):
    """Неудачный вызов квоту тратит, а счётчик не рос — защита была слепой.

    Ровно в сценарии сплошных 429, ради которого счётчик и написан, он говорил
    «лимит не исчерпан» и пускал бота долбиться дальше.
    """
    before = parse_log.calls_today()

    await phrase(llm.Answer(reason=llm.UNAVAILABLE, model="main/model", calls=2))

    assert parse_log.calls_today() == before + 2


@pytest.mark.asyncio
async def test_parse_log_names_the_model_that_answered(phrase, monkeypatch):
    """В лог писалась основная модель из настроек, а не ответившая.

    С цепочкой это прямое враньё: чинить будут не ту модель, которая подвела.
    """
    monkeypatch.setattr(capture.settings, "openrouter_model", "main/model")

    await phrase(
        llm.Answer(
            data={"intent": "create", "items": [{"kind": "task", "title": "молоко"}]},
            model="spare/model",
            tried=("main/model", "spare/model"),
            calls=2,
        )
    )

    parsed = [line for line in _log_lines() if '"via": "llm"' in line]
    assert parsed, "строка разбора обязана быть"
    assert '"model": "spare/model"' in parsed[-1]


@pytest.mark.asyncio
async def test_the_fallback_line_carries_the_reason(phrase):
    """По `parse.log` нельзя было отделить «модель ошиблась» от «не отвечала».

    Все строки `via=dateparser` были неотличимы, и материала для разбора
    полётов не оставалось никакого.
    """
    await phrase(llm.Answer(reason=llm.UNAVAILABLE, model="main/model", calls=1))

    fallback = [line for line in _log_lines() if '"via": "dateparser"' in line]
    assert fallback, "запасной разбор обязан оставить строку"
    assert f'"after": "{llm.UNAVAILABLE}"' in fallback[-1]


@pytest.mark.asyncio
async def test_a_successful_parse_still_says_nothing_about_ai(phrase):
    """Обратная половина: зелёный по обе стороны, краснеет от перестарания.

    Приписать «разобрал ИИ» к каждой карточке — соблазн ровно того же рода, что
    и молчать обо всём. Про ИИ говорим только тогда, когда его не было.
    """
    message = await phrase(
        llm.Answer(
            data={"intent": "create", "items": [{"kind": "task", "title": "молоко"}]},
            model="main/model",
            calls=1,
        )
    )

    card = message.texts[-1]
    assert "ИИ" not in card
    assert texts.ai_reason(llm.UNAVAILABLE) not in card
