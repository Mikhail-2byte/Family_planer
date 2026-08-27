"""Промпт и приведение ответа модели, шаг 3a.3.

Модуль чистый — ни БД, ни сети, ни aiogram, поэтому и тесты синхронные.
"""

from datetime import datetime

import pytest

from bot.services import parsing
from bot.services.parsing import Item, build_system, normalize

NOW = datetime(2026, 8, 27, 14, 30)  # четверг
MSK = "Europe/Moscow"


# --- промпт -------------------------------------------------------------------


def test_prompt_states_current_moment():
    """Без «сейчас» модель не разберёт ни «завтра», ни «в понедельник»."""
    system = build_system(NOW, MSK, ["Аня"], [])
    assert "четверг" in system
    assert "27 августа 2026" in system
    assert "14:30" in system
    assert MSK in system


def test_prompt_lists_members():
    system = build_system(NOW, MSK, ["Аня", "Миша"], [])
    assert "Аня, Миша" in system


def test_prompt_survives_empty_members_and_lists():
    """До этапа 4 списков нет вообще — это штатное состояние, а не сбой."""
    system = build_system(NOW, MSK, [], [])
    assert "списков пока нет" in system
    assert "участники не известны" in system


def test_prompt_skips_blank_names():
    assert "Аня" in build_system(NOW, MSK, ["Аня", "", "   "], [])


def test_prompt_carries_schema():
    system = build_system(NOW, MSK, ["Аня"], [])
    for field in ("intent", "items", "kind", "due_at", "all_day", "rrule",
                  "confidence", "reminders"):
        assert field in system, field


def test_prompt_does_not_ask_for_dead_fields():
    """`list` и `assignee` из схемы PLAN.md выброшены до этапов 4 и «исполнитель».

    Спрашивать поле, которое `normalize` всё равно не читает, — платить
    токенами за приглашение галлюцинировать.
    """
    system = build_system(NOW, MSK, ["Аня"], [])
    assert '"assignee"' not in system
    assert '"list"' not in system


# --- normalize: общая форма ---------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], "строка", 42, {}])
def test_garbage_gives_chitchat_and_no_items(raw):
    """Худшее, что делает неразобранный ответ, — заставляет бота промолчать."""
    assert normalize(raw) == ("chitchat", [])


def test_unknown_intent_becomes_chitchat():
    intent, _ = normalize({"intent": "выполнить", "items": []})
    assert intent == "chitchat"


def test_items_not_a_list_is_survived():
    """Intent сохраняется: он разобрался, даже если items сломаны."""
    assert normalize({"intent": "create", "items": "молоко"}) == ("create", [])


def test_items_are_capped():
    raw = {"intent": "create", "items": [{"title": f"дело {i}"} for i in range(50)]}
    _, items = normalize(raw)
    assert len(items) == parsing.MAX_ITEMS


# --- normalize: один элемент --------------------------------------------------


def _one(**fields) -> Item | None:
    _, items = normalize({"intent": "create", "items": [{"title": "Молоко", **fields}]})
    return items[0] if items else None


def test_full_item_is_parsed():
    _, items = normalize(
        {
            "intent": "create",
            "items": [
                {
                    "kind": "shopping",
                    "title": "  Купить   молоко ",
                    "body": "два литра",
                    "due_at": "2026-08-28T19:00:00",
                    "all_day": False,
                    "reminders": [{"at": "2026-08-28T18:00:00"}],
                    "rrule": None,
                    "confidence": 0.93,
                }
            ],
        }
    )
    item = items[0]
    assert item.kind == "shopping"
    assert item.title == "Купить молоко"  # пробелы схлопнуты
    assert item.body == "два литра"
    assert item.due_at == datetime(2026, 8, 28, 19, 0)
    assert item.reminders == (datetime(2026, 8, 28, 18, 0),)
    assert item.confidence == pytest.approx(0.93)
    assert not item.uncertain


@pytest.mark.parametrize("bad", [None, "", "   ", 42, {"a": 1}])
def test_item_without_title_is_dropped(bad):
    """Показывать в карточке и сохранять нечего — элемента просто нет."""
    _, items = normalize({"intent": "create", "items": [{"title": bad}]})
    assert items == []


def test_non_dict_item_is_dropped():
    _, items = normalize({"intent": "create", "items": ["молоко", None, 7]})
    assert items == []


def test_good_item_survives_broken_neighbour():
    _, items = normalize(
        {"intent": "create", "items": [{"title": ""}, {"title": "Хлеб"}]}
    )
    assert [i.title for i in items] == ["Хлеб"]


@pytest.mark.parametrize("kind", ["покупка", None, "", "urgent", 5])
def test_unknown_kind_falls_back_to_task(kind):
    assert _one(kind=kind).kind == "task"


def test_title_is_truncated():
    assert len(_one(title="я" * 900).title) == parsing.TITLE_LIMIT


# --- normalize: время ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [None, "", "завтра в 19", "28.08.2026 19:00", 20260828, []]
)
def test_broken_due_at_becomes_none(bad):
    item = _one(due_at=bad)
    assert item.due_at is None
    # Без даты «весь день» ничего не значит и только сбивало бы карточку
    assert item.all_day is False


def test_offset_is_dropped_not_converted():
    """Просили местное время. Пришло со смещением — доверять ему нельзя.

    Из какой зоны модель взяла смещение, неизвестно; пересчёт по нему уводил бы
    срок на часы. Берём то, что написано на часах: зону семьи применит хендлер.
    """
    assert _one(due_at="2026-08-28T19:00:00+05:00").due_at == datetime(2026, 8, 28, 19)
    assert _one(due_at="2026-08-28T19:00:00Z").due_at == datetime(2026, 8, 28, 19)


def test_date_without_time_is_all_day_even_without_the_flag():
    """Иначе запись отрендерится сроком «00:00», которого никто не называл."""
    item = _one(due_at="2026-08-28", all_day=False)
    assert item.due_at == datetime(2026, 8, 28)
    assert item.all_day is True


def test_all_day_flag_is_respected():
    assert _one(due_at="2026-08-28T00:00:00", all_day=True).all_day is True
    assert _one(due_at="2026-08-28T19:00:00", all_day=True).all_day is True


# --- normalize: напоминания, повтор, уверенность ------------------------------


def test_reminders_accept_both_shapes():
    """Схема просит `{"at": ...}`, но голую строку модель присылает охотно."""
    assert _one(reminders=[{"at": "2026-08-28T18:00:00"}]).reminders == (
        datetime(2026, 8, 28, 18),
    )
    assert _one(reminders=["2026-08-28T18:00:00"]).reminders == (
        datetime(2026, 8, 28, 18),
    )


@pytest.mark.parametrize("bad", [None, "скоро", 5, [{"at": "потом"}], [{}], [None]])
def test_broken_reminders_are_dropped(bad):
    assert _one(reminders=bad).reminders == ()


def test_reminders_are_capped():
    many = [{"at": f"2026-08-28T{h:02d}:00:00"} for h in range(20)]
    assert len(_one(reminders=many).reminders) == parsing.MAX_REMINDERS


def test_rrule_is_kept_when_it_looks_like_one():
    assert _one(rrule="FREQ=WEEKLY;BYDAY=TU").rrule == "FREQ=WEEKLY;BYDAY=TU"


@pytest.mark.parametrize("bad", [None, "", "каждый вторник", 7, "BYDAY=TU"])
def test_rrule_without_freq_is_refused(bad):
    """Без `FREQ=` `dateutil` откажет всё равно — но уже в тикере, молча."""
    assert _one(rrule=bad).rrule is None


@pytest.mark.parametrize(
    "raw, expected", [(0.93, 0.93), (1, 1.0), (0, 0.0), (5, 1.0), (-2, 0.0)]
)
def test_confidence_is_clamped(raw, expected):
    assert _one(confidence=raw).confidence == pytest.approx(expected)


@pytest.mark.parametrize("bad", [None, "высокая", True, False, []])
def test_broken_confidence_becomes_zero(bad):
    """`True` — подкласс int, и без отдельной проверки стал бы уверенностью 1.0."""
    assert _one(confidence=bad).confidence == 0.0


def test_low_confidence_is_flagged():
    assert _one(confidence=0.2).uncertain
    assert not _one(confidence=0.9).uncertain
