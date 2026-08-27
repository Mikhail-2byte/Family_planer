"""Промпт для модели и приведение её ответа к записям (шаг 3a.3).

`llm.py` — транспорт: он знает про OpenRouter, ретраи и то, как достать JSON из
ответа. Здесь домен: что именно спросить («сегодня четверг, 27 августа, в семье
Аня и Миша») и как превратить пришедший JSON в то, что можно класть в базу.
На шаге 3b.7 сюда же ляжет промпт дешёвого триажа.

Модуль чистый: ни БД, ни aiogram, ни таймзон. Всё время внутри — **локальное
наивное время семьи**, ровно в том виде, в каком его выдала модель; перевод в
UTC делает хендлер через `timeutil.to_utc`.

`normalize` — вторая линия обороны после `llm.extract_json`. Тот отвечает лишь
за то, что ответ вообще является объектом JSON; за то, что внутри осмысленные
значения, не отвечает никто: выбранная модель `minimax/minimax-m3:free`
structured outputs не поддерживает и держит схему «на слово» (шаг 3a.1).
Поэтому здесь каждое поле проверяется по отдельности, а негодный элемент
выбрасывается, а не роняет разбор целиком.
"""

from dataclasses import dataclass
from datetime import date, datetime

KINDS = ("task", "note", "event", "shopping")
INTENTS = ("create", "query", "complete", "chitchat")
DEFAULT_KIND = "task"
DEFAULT_INTENT = "chitchat"  # худшее, что делает неразобранный ответ, — молчание

TITLE_LIMIT = 500  # столько же, сколько в entries.title
BODY_LIMIT = 1000
RRULE_LIMIT = 255  # столько же, сколько в reminders.rrule
MAX_ITEMS = 10  # одна фраза столько записей не даёт — это защита от зациклившейся модели
MAX_REMINDERS = 5

# Границы осмысленной даты. За ними лежит не план семьи, а мусор модели —
# `0001-01-01` она охотно отдаёт вместо «даты нет». И это не только бессмыслица:
# перевод такой даты в UTC даёт `OverflowError`, то есть падение хендлера ещё
# до показа карточки
MIN_YEAR = 2000
MAX_YEAR = 2100

# Порог, ниже которого разбор помечается в карточке как сомнительный.
# Модель ставит 0.9+, когда дата и тип названы прямо, и заметно меньше на
# фразах вроде «надо бы как-нибудь съездить к маме»
LOW_CONFIDENCE = 0.5

# Полные названия — для модели, а не для показа человеку: «пн» она понимает
# хуже, чем «понедельник», а ошибка в дне недели уводит всю дату
_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


@dataclass(frozen=True, slots=True)
class Item:
    """Одна разобранная запись. Времена — локальные наивные, не UTC."""

    kind: str
    title: str
    body: str | None = None
    due_at: datetime | None = None
    all_day: bool = False
    reminders: tuple[datetime, ...] = ()
    rrule: str | None = None
    confidence: float = 0.0

    @property
    def uncertain(self) -> bool:
        return self.confidence < LOW_CONFIDENCE


SCHEMA = """{
  "intent": "create|query|complete|chitchat",
  "items": [
    {
      "kind": "task|note|event|shopping",
      "title": "Купить молоко",
      "body": null,
      "due_at": "2026-08-28T19:00:00",
      "all_day": false,
      "reminders": [{"at": "2026-08-28T18:00:00"}],
      "rrule": null,
      "confidence": 0.93
    }
  ]
}"""

# Поля `list` и `assignee` из схемы PLAN.md здесь намеренно отсутствуют.
# Списков до этапа 4 не существует, а сопоставления имени с `members` нет
# нигде — просить у модели поле, которое гарантированно будет отброшено,
# значит платить токенами за приглашение галлюцинировать. Вернутся вместе
# с этапом 4 и пунктом «исполнитель».

_RULES = """Правила разбора:
- intent: create — просят записать, запланировать, купить, напомнить;
  query — спрашивают, что запланировано; complete — сообщают, что дело сделано;
  chitchat — всё остальное, включая обычную болтовню и вопросы не о планах.
  Для query, complete и chitchat оставляй items пустым.
- Всё время — местное время семьи в формате YYYY-MM-DDTHH:MM:SS, без смещения
  и без буквы Z. Никогда не UTC.
- Относительные даты («завтра», «в понедельник», «через час») считай от «сейчас»
  выше. Если названы день и месяц без года, бери ближайшую будущую дату.
- Назван день, но не время — all_day: true, а due_at на 00:00 этого дня.
- Ни дня, ни времени не названо — due_at: null, all_day: false.
- reminders заполняй, только если о напоминании попросили явно: «напомни»,
  «предупреди», «за час до». «к 19» и «в 19» — это срок, а не напоминание.
- Повторяемость («каждый вторник», «по будням», «ежедневно») — в rrule по
  RFC 5545, без DTSTART: FREQ=WEEKLY;BYDAY=TU;BYHOUR=19;BYMINUTE=0.
- kind: shopping — продукты и покупки; event — встречи, приёмы, поездки,
  у которых есть время; note — то, что просто нужно запомнить; task — остальное.
- title — короткая формулировка словами человека, без даты и времени внутри.
- confidence — от 0 до 1: насколько ты уверен в разборе.
- Одна фраза может дать несколько записей: «купи молока и хлеба» — это два items.
Отвечай только JSON по схеме, без пояснений и без markdown-обёртки."""


def build_system(
    now_local: datetime, tz: str, members: list[str], lists: list[str]
) -> str:
    """Системный промпт: схема плюс всё, что модель не может знать сама."""
    stamp = (
        f"{_WEEKDAYS[now_local.weekday()]}, {now_local.day} "
        f"{_MONTHS[now_local.month - 1]} {now_local.year} года, "
        f"{now_local:%H:%M}"
    )
    return "\n\n".join(
        [
            "Ты разбираешь сообщения из семейного чата в записи планировщика.",
            f"Сейчас: {stamp}. Таймзона семьи: {tz}.\n"
            f"Участники семьи: {_listing(members, 'участники не известны')}.\n"
            f"Списки: {_listing(lists, 'списков пока нет')}.",
            f"Схема ответа:\n{SCHEMA}",
            _RULES,
        ]
    )


def _listing(values: list[str], empty: str) -> str:
    """Перечисление через запятую. Пустой список — фраза, а не пустое место.

    До этапа 4 списков нет вообще, и подставленная сюда пустота читалась бы
    моделью как оборванный промпт (требование 3a.3 — «не падать»).
    """
    cleaned = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return ", ".join(cleaned) if cleaned else empty


def normalize(raw: dict | None) -> tuple[str, list[Item]]:
    """Ответ модели → `(intent, items)`. Всё непонятное отбрасывается молча."""
    if not isinstance(raw, dict):
        return DEFAULT_INTENT, []

    intent = raw.get("intent")
    intent = intent if intent in INTENTS else DEFAULT_INTENT

    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return intent, []

    items = [_item(one) for one in raw_items[:MAX_ITEMS]]
    return intent, [one for one in items if one is not None]


def _item(raw: object) -> Item | None:
    if not isinstance(raw, dict):
        return None

    title = _text(raw.get("title"), TITLE_LIMIT)
    if title is None:
        # Запись без заголовка нечего показывать в карточке и незачем сохранять
        return None

    due_at = _dt(raw.get("due_at"))
    kind = raw.get("kind")
    return Item(
        kind=kind if kind in KINDS else DEFAULT_KIND,
        title=title,
        body=_text(raw.get("body"), BODY_LIMIT),
        due_at=due_at,
        # Дата без времени — это «весь день», даже если модель забыла флаг:
        # иначе запись отрендерится сроком «00:00», которого никто не называл
        all_day=due_at is not None
        and (bool(raw.get("all_day")) or _date_only(raw.get("due_at"))),
        reminders=_reminders(raw.get("reminders")),
        rrule=_rrule(raw.get("rrule")),
        confidence=_confidence(raw.get("confidence")),
    )


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:limit]
    return cleaned or None


def _dt(value: object) -> datetime | None:
    """Строка ISO-8601 → наивный datetime. Мусор — `None`, не исключение."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return None
    # Просили местное время без смещения. Если смещение всё же пришло, доверять
    # ему нельзя — неизвестно, из какой зоны модель его взяла. Берём то, что
    # написано на часах: дальше хендлер всё равно переведёт в UTC по зоне семьи
    return parsed.replace(tzinfo=None)


def _date_only(value: object) -> bool:
    """Дата без времени вообще.

    Проверять отсутствие `T` нельзя: ISO допускает и пробел-разделитель, а
    `datetime.fromisoformat` его принимает — «2026-08-28 19:00» тогда молча
    становилось бы записью на весь день, и названное время пропадало.
    `date.fromisoformat` строг: он принимает только чистую дату.
    """
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def _reminders(value: object) -> tuple[datetime, ...]:
    """`[{"at": "..."}]` по схеме, но и голую строку модель присылает охотно."""
    if not isinstance(value, list):
        return ()
    moments = []
    for one in value[:MAX_REMINDERS]:
        parsed = _dt(one.get("at") if isinstance(one, dict) else one)
        if parsed is not None:
            moments.append(parsed)
    return tuple(moments)


def _rrule(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    # `FREQ=` — минимальный признак настоящего RRULE. Без него `dateutil`
    # откажет всё равно, но уже в тикере, где отказ увидит только лог.
    # Слишком длинное правило отвергается, а не обрезается: обрезка может дать
    # синтаксически верное правило с другим смыслом — тихую подмену серии
    if "FREQ=" not in cleaned.upper() or len(cleaned) > RRULE_LIMIT:
        return None
    return cleaned


def _confidence(value: object) -> float:
    # bool — подкласс int, и `True` превратился бы в уверенность 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(value)))
