"""Лог разбора: счётчик суточных обращений и ротация.

Своего файла у `parse_log` не было — он проверялся косвенно, через `capture` и
`voice`. Появился вместе с двумя вещами, которые косвенно не проверить.

**Счётчик.** У бесплатного тира OpenRouter лимит на аккаунт, и именно из-за
него отменён режим `all`. Учёта при этом не было никакого: упёршись в лимит,
бот молча уходил на `dateparser`, человек видел «разобрал без ИИ» и думал, что
сломалась сеть.

**Ротация.** В файл уходит полный текст человека и полный ответ модели. Он
нужен, чтобы подкручивать промпт, но хранить семейную переписку годами ради
этого незачем — тот же довод, по которому ежедневный бэкап не уходит в облако.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from bot.services import parse_log


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """Свой лог и обнулённый счётчик на каждый тест.

    Автофикстура `_parse_log` в `conftest.py` делает то же самое, но полагаться
    на её внутренности из соседнего файла — значит завязать один тест на другой.
    """
    monkeypatch.setattr(parse_log, "PATH", tmp_path / "parse.log")
    monkeypatch.setattr(parse_log, "_counted_day", None)
    monkeypatch.setattr(parse_log, "_counted", 0)
    return tmp_path


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writes_a_json_line_with_a_stamp(_fresh):
    parse_log.write(event="parse", via="llm", text="привет")

    records = _lines(parse_log.PATH)
    assert len(records) == 1
    assert records[0]["text"] == "привет"
    assert records[0]["at"].startswith(datetime.now(UTC).date().isoformat())


def test_counts_only_llm_parses(_fresh):
    parse_log.write(event="parse", via="llm", text="раз")
    parse_log.write(event="parse", via="llm", text="два")
    # Запасной разбор модели не стоил ничего
    parse_log.write(event="parse", via="dateparser", text="три")
    # Голос уходит в Groq — у него своя квота, ради этого он и разведён
    parse_log.write(event="voice", model="whisper", duration=8)
    parse_log.write(event="verdict", verdict="saved")

    assert parse_log.calls_today() == 2


def test_counter_survives_a_restart(_fresh):
    """Домашний ПК перезапускается часто, а лимит провайдера — нет."""
    parse_log.write(event="parse", via="llm", text="раз")
    parse_log.write(event="parse", via="llm", text="два")

    # «Перезапуск»: память чиста, файл на месте
    parse_log._counted_day = None
    parse_log._counted = 0

    assert parse_log.calls_today() == 2


def test_yesterdays_calls_do_not_count(_fresh):
    """Иначе к концу недели бот считал бы себя навсегда упёршимся в лимит."""
    stale = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    parse_log.PATH.write_text(
        json.dumps(
            {"event": "parse", "via": "llm", "at": stale.isoformat(timespec="seconds")},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert parse_log.calls_today() == 0


def test_a_broken_line_does_not_stop_the_count(_fresh):
    """Файл дописывается на живой машине — оборванная строка возможна."""
    today = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    good = json.dumps({"event": "parse", "via": "llm", "at": today})
    parse_log.PATH.write_text(f"{good}\n{{обрыв\n{good}\n", encoding="utf-8")

    assert parse_log.calls_today() == 2


def test_missing_file_counts_as_zero(_fresh):
    assert not parse_log.PATH.exists()
    assert parse_log.calls_today() == 0


def test_rotation_moves_the_log_aside(_fresh, monkeypatch):
    monkeypatch.setattr(parse_log, "MAX_BYTES", 200)

    for i in range(50):
        parse_log.write(event="parse", via="llm", text=f"фраза номер {i}")

    rotated = parse_log.PATH.with_suffix(parse_log.PATH.suffix + ".1")
    assert rotated.exists(), "разросшийся лог обязан уехать в .1"
    assert parse_log.PATH.stat().st_size < 400, "а на его месте — новый, короткий"


def test_rotation_keeps_exactly_one_generation(_fresh, monkeypatch):
    """Одно поколение, а не пять: в файле лежит переписка семьи."""
    monkeypatch.setattr(parse_log, "MAX_BYTES", 200)

    for i in range(200):
        parse_log.write(event="parse", via="llm", text=f"фраза номер {i}")

    siblings = sorted(p.name for p in _fresh.iterdir())
    assert siblings == ["parse.log", "parse.log.1"]


def test_counter_survives_rotation(_fresh, monkeypatch):
    """Счётчик в памяти, и перекладывание файла лимит обнулять не должно."""
    monkeypatch.setattr(parse_log, "MAX_BYTES", 200)

    for i in range(50):
        parse_log.write(event="parse", via="llm", text=f"фраза номер {i}")

    assert parse_log.calls_today() == 50


def test_write_never_raises(_fresh, monkeypatch):
    """Упавшая запись в лог не должна стоить человеку карточки.

    Полный диск изображаем каталогом на месте файла: открыть его на запись
    нельзя, а подменять `Path.open` глобально — значит сломать и сам pytest.
    """
    monkeypatch.setattr(parse_log, "PATH", _fresh / "занято")
    parse_log.PATH.mkdir()

    parse_log.write(event="parse", via="llm", text="фраза")  # не бросает
    # А счётчик всё равно увеличился: вызов к модели состоялся, что бы ни
    # случилось с логом
    assert parse_log._counted == 1


# --- Этап 12: счёт в запросах, а не в строках --------------------------------
#
# До этапа 12 строка писалась только после удачного разбора, то есть неудачный
# вызов квоту у провайдера тратил, а счётчик не растил. Защита не срабатывала
# ровно в сценарии сплошных 429, ради которого написана. С цепочкой моделей
# одна фраза стоит до девяти запросов, и счёт по строкам разошёлся бы в разы.


def test_calls_are_counted_not_lines(_fresh):
    parse_log.write(event="parse", via="llm", text="раз", calls=3)

    assert parse_log.calls_today() == 3


def test_old_lines_without_calls_still_count_as_one(_fresh):
    """Лог переживает обновление бота: строки этапа 3b поля `calls` не знают."""
    parse_log.PATH.write_text(
        json.dumps(
            {
                "event": "parse",
                "via": "llm",
                "at": datetime.now(UTC).replace(tzinfo=None).isoformat(
                    timespec="seconds"
                ),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert parse_log.calls_today() == 1


def test_a_failed_call_counts_too(_fresh):
    """Неудачный вызов провайдер тоже считает — иначе защита слепа."""
    parse_log.write(event="parse", via="llm", result="failed", reason="unavailable", calls=2)

    assert parse_log.calls_today() == 2


def test_a_skipped_call_costs_nothing(_fresh):
    """Ветка квоты никуда не ходила — списывать с неё нечего."""
    parse_log.write(event="parse", via="llm", result="skipped", reason="quota", calls=0)

    assert parse_log.calls_today() == 0


def test_probe_counts_against_the_daily_limit(_fresh):
    """Иначе собственный лимит обходился бы командой /ai."""
    parse_log.write(event="probe", via="llm", ok=True, calls=1)

    assert parse_log.calls_today() == 1


def test_a_broken_calls_field_does_not_break_the_count(_fresh):
    parse_log.write(event="parse", via="llm", calls="много")

    assert parse_log.calls_today() == 0


def test_quota_spent_reads_the_limit_from_settings(_fresh, monkeypatch):
    monkeypatch.setattr(parse_log.settings, "llm_daily_limit", 2)
    assert parse_log.quota_spent() is False

    parse_log.write(event="parse", via="llm", calls=2)
    assert parse_log.quota_spent() is True


def test_zero_limit_never_counts_as_spent(_fresh, monkeypatch):
    """Платный ключ суточного потолка не имеет — проверка обязана выключаться."""
    monkeypatch.setattr(parse_log.settings, "llm_daily_limit", 0)
    parse_log.write(event="parse", via="llm", calls=10_000)

    assert parse_log.quota_spent() is False


# --- Этап 12: последний отказ для /ai ----------------------------------------


def test_last_failure_finds_the_newest_refusal(_fresh):
    parse_log.write(event="parse", via="llm", reason="unavailable", detail="401 старое")
    parse_log.write(event="parse", via="llm", reason="bad_json", detail="мусор")

    record = parse_log.last_failure()

    assert record is not None
    assert record["reason"] == "bad_json"


def test_last_failure_ignores_the_fallback_line(_fresh):
    """Строка запасного разбора носит поле `after` и заслонять отказ не должна.

    Она пишется следом за отказом, в ту же секунду, — по времени она новее, и
    без разницы в именах полей подробности провайдера были бы недостижимы.
    """
    parse_log.write(event="parse", via="llm", reason="unavailable", detail="401 GMICloud")
    parse_log.write(event="parse", via="dateparser", result="ok", after="unavailable")

    record = parse_log.last_failure()

    assert record is not None
    assert record["detail"] == "401 GMICloud"


def test_last_failure_ignores_a_successful_parse(_fresh):
    parse_log.write(event="parse", via="llm", intent="create", items=1)

    assert parse_log.last_failure() is None


def test_last_failure_without_a_log_is_none(_fresh):
    assert parse_log.last_failure() is None
