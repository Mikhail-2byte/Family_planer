"""Команда `/ai` — проверка связи с ИИ (этап 12).

Заведена после 01.09.2026: единственный провайдер бесплатной модели ответил
401, разбор молча уехал на `dateparser`, и понять «сломан ИИ или нет» было
неоткуда — в чате обе беды выглядели одинаково, а лог лежит на машине бота.

Сети тут нет: `llm.ask` подменяется целиком, как в `test_capture.py`.
"""

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot import texts
from bot.handlers import admin
from bot.services import llm, parse_log


class FakeProbe:
    """Сообщение «Проверяю…», которое потом правят отчётом."""

    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeMessage:
    """Команда человека. `answer` возвращает сообщение, как настоящий."""

    def __init__(self, chat_id: int = -1001):
        self.chat = SimpleNamespace(id=chat_id, type="supergroup")
        self.replies: list[str] = []
        self._next_id = 700

    async def answer(self, text: str, **kwargs):
        self.replies.append(text)
        self._next_id += 1
        return FakeProbe(self._next_id)

    @property
    def texts(self) -> list[str]:
        return list(self.replies)


@pytest.fixture
def asked(monkeypatch):
    """Подменяет обращение к модели. Возвращает счётчик вызовов."""
    calls: list[tuple[str, str]] = []

    def use(answer):
        async def fake_ask(system, user, **kwargs):
            calls.append((system, user))
            return answer

        monkeypatch.setattr(admin.llm, "ask", fake_ask)
        return calls

    return use


def _up(model="main/model", tried=("main/model",), elapsed=2.7):
    return llm.Answer(data={"ok": 1}, model=model, tried=tried, calls=1, elapsed=elapsed)


def _down(detail="401 Provider returned error", tried=("main/model", "spare/model")):
    return llm.Answer(
        reason=llm.UNAVAILABLE, model=tried[-1], tried=tried, detail=detail, calls=2
    )


def _report(bot, message) -> str:
    """Отчёт: правкой «Проверяю…», а если правка не прошла — отдельным ответом."""
    if bot.edited:
        return bot.edited[-1][2]
    return message.texts[-1]


@pytest.mark.asyncio
async def test_says_it_is_checking_before_going_anywhere(family, bot, monkeypatch):
    """Живой запрос занимает секунды — молчание читалось бы как зависание."""
    message = FakeMessage(family.chat_id)
    seen: list[list[str]] = []

    async def fake_ask(system, user, **kwargs):
        # Что уже лежит в чате к моменту похода в сеть
        seen.append(message.texts)
        return _up()

    monkeypatch.setattr(admin.llm, "ask", fake_ask)

    await admin.cmd_ai(message, family, bot)

    assert seen == [[texts.AI_CHECKING]]


@pytest.mark.asyncio
async def test_reports_the_model_that_answered(asked, family, bot):
    asked(_up(model="spare/model", tried=("main/model", "spare/model")))

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    report = _report(bot, message)
    assert "spare/model" in report
    assert texts.ai_after_fallback("main/model") in report, (
        "молчание основной — это и есть повод чинить, о нём надо сказать"
    )


@pytest.mark.asyncio
async def test_says_nothing_about_a_spare_when_the_main_answered(asked, family, bot):
    asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert "запасной" not in _report(bot, message)


@pytest.mark.asyncio
async def test_reports_the_provider_text_when_ai_is_down(asked, family, bot):
    """Весь диагноз 01.09.2026 был в теле ответа провайдера."""
    asked(_down())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    report = _report(bot, message)
    assert "401" in report
    assert "Provider returned error" in report
    assert "main/model" in report and "spare/model" in report


@pytest.mark.asyncio
async def test_provider_text_is_escaped(asked, family, bot):
    """`<` в чужом тексте — это `can't parse entities`, то есть потеря отчёта.

    Отчёт о поломке, который сам не доходит из-за поломки, — худший из исходов.
    """
    asked(_down(detail="401 <b>nope</b>"))

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    report = _report(bot, message)
    assert "&lt;b&gt;nope&lt;/b&gt;" in report
    assert "<b>nope</b>" not in report


@pytest.mark.asyncio
async def test_quota_spent_means_no_live_call(asked, family, bot, monkeypatch):
    """Разбор в этом состоянии мы тоже не шлём — иначе лимит обходится командой."""
    monkeypatch.setattr(parse_log.settings, "llm_daily_limit", 2)
    monkeypatch.setattr(parse_log, "_counted_day", parse_log._today())
    monkeypatch.setattr(parse_log, "_counted", 2)
    calls = asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert calls == []
    assert texts.ai_quota(2, 2) in _report(bot, message)


@pytest.mark.asyncio
async def test_probe_is_written_to_the_log(asked, family, bot):
    """Проба — настоящий запрос к аккаунту, и в счётчике ей место."""
    asked(_up())

    await admin.cmd_ai(FakeMessage(family.chat_id), family, bot)

    records = [
        line
        for line in parse_log.PATH.read_text(encoding="utf-8").splitlines()
        if '"probe"' in line
    ]
    assert len(records) == 1
    assert parse_log.calls_today() == 1


@pytest.mark.asyncio
async def test_voice_is_reported_by_config_not_by_a_live_call(
    asked, family, bot, monkeypatch
):
    """Аудио для пробы нет, а пустой файл эндпоинт отвергает 400.

    «Проверка», которая не отличает рабочий ключ от нерабочего, зато тратит
    чужую квоту и секунды ожидания, — хуже честной строки из конфига.
    """
    monkeypatch.setattr(admin.settings, "stt_key", "")
    asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert texts.AI_VOICE_OFF in _report(bot, message)

    monkeypatch.setattr(admin.settings, "stt_key", "gsk-test")
    monkeypatch.setattr(admin.settings, "stt_model", "whisper-large-v3-turbo")
    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert texts.ai_voice("whisper-large-v3-turbo") in _report(bot, message)


@pytest.mark.asyncio
async def test_last_failure_is_shown(asked, family, bot):
    parse_log.write(event="parse", via="llm", reason=llm.UNAVAILABLE, detail="401")
    asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert "Последний отказ" in _report(bot, message)


@pytest.mark.asyncio
async def test_no_failures_is_said_out_loud(asked, family, bot):
    """Пустое место читалось бы как «не проверял»."""
    asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert texts.AI_NO_FAILURES in _report(bot, message)


@pytest.mark.asyncio
async def test_report_replaces_the_checking_message(asked, family, bot):
    """Два сообщения на команду — лишний шум, который потом ещё и убирать."""
    asked(_up())
    message = FakeMessage(family.chat_id)

    await admin.cmd_ai(message, family, bot)

    assert len(bot.edited) == 1
    assert message.texts == [texts.AI_CHECKING], "второго сообщения быть не должно"


@pytest.mark.asyncio
async def test_report_still_arrives_when_the_edit_fails(asked, family, monkeypatch):
    """Правка может не пройти — сообщение успели стереть уборкой."""
    from tests.conftest import FakeBot

    broken = FakeBot(
        fail_on_edit={
            0: TelegramBadRequest(
                method=SimpleNamespace(), message="message to edit not found"
            )
        }
    )
    asked(_up())
    message = FakeMessage(family.chat_id)

    await admin.cmd_ai(message, family, broken)

    assert len(message.texts) == 2, "отчёт обязан дойти хотя бы новым сообщением"
    assert "main/model" in message.texts[-1]


def test_ai_is_in_the_command_menu():
    """Команда, которой нет в меню, — команда, о которой никто не узнает."""
    assert any(name == "ai" for name, _ in texts.COMMANDS)


@pytest.mark.asyncio
async def test_a_fresh_failure_is_not_repeated_as_history(asked, family, bot):
    """Свежайший отказ — это сама проба; «последний отказ: только что» лишнее."""
    asked(_down())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    report = _report(bot, message)
    assert "Последний отказ" not in report
    assert texts.AI_NO_FAILURES not in report


@pytest.mark.asyncio
async def test_history_is_shown_when_the_check_succeeds(asked, family, bot):
    """А вот «сегодня утром отказывал, сейчас жив» — знание, которого не было."""
    parse_log.write(event="parse", via="llm", reason=llm.UNAVAILABLE, detail="401")
    asked(_up())

    await admin.cmd_ai(message := FakeMessage(family.chat_id), family, bot)

    assert "Последний отказ" in _report(bot, message)


def test_every_reason_has_a_phrase():
    """Сторож против «завёл причину, забыл текст».

    `texts.ai_reason` на незнакомый ключ отвечает общей фразой, то есть новая
    причина не сломает бота, а молча промолчит о себе — ровно та безликость,
    ради избавления от которой затеян этап 12.
    """
    reasons = {llm.UNAVAILABLE, llm.BAD_JSON, llm.NO_KEY, llm.NO_MODEL, llm.QUOTA}

    missing = reasons - set(texts.AI_REASONS)

    assert not missing, f"причины без фразы: {sorted(missing)}"
