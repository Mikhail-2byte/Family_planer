"""Разбор «когда» из свободного текста без LLM.

Запасной путь для `/remind` (этап 2) и для случая, когда OpenRouter недоступен
(этап 3b). Модуль чистый: ни БД, ни aiogram, «сейчас» приходит параметром.

Почему тут столько ручной работы вокруг `dateparser`: на русском он **молча
возвращает неверную дату**, а не отказ. Замерено на живых фразах:

    «завтра в 19»        → завтра 12:00   (час потерян)
    «в понедельник в 9»  → 9-е число      («9» принято за день месяца)
    «1 сентября в 10»    → 2110 год       («10» принято за год)
    «12.03 в 8»          → 8 августа      (дата принята за время)

Для напоминания это худший исход: бот выстрелит не тогда и никто не заметит.
Поэтому здесь три приёма — нормализация часов, поиск фрагмента вместо разбора
всей строки и проверка границ фрагмента.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time

from dateparser.search import search_dates

from bot.services.parsing import MAX_YEAR, MIN_YEAR

_SETTINGS = {
    "PREFER_DATES_FROM": "future",
    "RETURN_AS_TIMEZONE_AWARE": False,
    "DATE_ORDER": "DMY",
}

# «каждый вторник», «ежедневно», «по будням» — повторяемость. dateparser её не
# понимает в принципе и разбирает такую фразу в РАЗОВОЕ напоминание, выбрасывая
# из текста слово «вторник». Лучше честно отказать.
#
# Ищем по всей строке, а не только в начале: порядок слов в /remind свободный,
# и «позвонить маме каждый вторник» — ровно тот же случай, что «каждый вторник
# позвонить маме». С якорем на начале первая форма молча становилась разовым
# напоминанием на ближайший вторник, а слово «каждый» оставалось в тексте.
_RECURRING = re.compile(
    r"\b(кажд\w+|ежедневн\w*|еженедельн\w*|ежемесячн\w*|ежегодн\w*"
    r"|по\s+(будням|выходным|понедельникам|вторникам|средам|четвергам"
    r"|пятницам|субботам|воскресеньям))\b",
    re.IGNORECASE,
)

# «в 19» → «в 19:00». Без этого час просто теряется.
# Ограничитель справа не пускает сюда «в 19:30» и «12.03».
_BARE_HOUR = re.compile(r"\bв (\d{1,2})(?![:.\d])")

# «в 7 вечера» dateparser не понимает вообще (возвращает None), поэтому
# переводим части суток в часы сами.
_DAYPART = re.compile(r"\bв (\d{1,2})\s+(утра|дня|вечера|ночи)\b", re.IGNORECASE)


@dataclass(slots=True)
class Parsed:
    when: datetime  # локальное время семьи, наивное
    text: str  # остаток строки — то, о чём напомнить
    fragment: str  # что именно распозналось как дата


def looks_recurring(text: str) -> bool:
    return bool(_RECURRING.search(text))


def _daypart_to_hour(match: re.Match) -> str:
    hour, part = int(match.group(1)), match.group(2).lower()
    if part in ("вечера", "дня") and hour < 12:
        hour += 12
    elif part == "ночи" and hour == 12:
        hour = 0
    return f"в {hour:02d}:00"


def normalize(raw: str) -> str:
    """Привести часы к виду, который `dateparser` понимает однозначно."""
    text = _DAYPART.sub(_daypart_to_hour, raw.strip())
    return _BARE_HOUR.sub(r"в \1:00", text)


def parse_when(raw: str, now_local: datetime) -> Parsed | None:
    """Выделить дату и остаток текста. None — не разобрали.

    `now_local` — «сейчас» в таймзоне семьи; результат тоже локальный, перевод
    в UTC остаётся на вызывающем.
    """
    text = normalize(raw)
    if not text:
        return None

    try:
        found = search_dates(
            text, languages=["ru"], settings={**_SETTINGS, "RELATIVE_BASE": now_local}
        )
    except Exception:
        # dateparser спотыкается на некоторых строках — для нас это «не понял»
        return None
    if not found:
        return None

    fragment, when = found[0]
    start = text.lower().find(fragment.lower())
    if start < 0:
        return None

    # Фрагмент обязан начинаться и кончаться на границе слова. Иначе
    # `search_dates` вырезает «03 в 8» из «12.03 в 8» и уезжает в другой год,
    # а остаток текста превращается в «сдать анализы 12.»
    before = text[start - 1] if start > 0 else " "
    end = start + len(fragment)
    after = text[end] if end < len(text) else " "
    if not before.isspace() or not after.isspace():
        return None

    # Границы года — та же защита, что у `parsing._dt` на пути модели, и по той
    # же причине: за ними лежит не план семьи, а мусор, а перевод такой даты в
    # UTC даёт `OverflowError`, то есть падение хендлера ещё до карточки.
    # `dateparser` уезжает туда охотно — «1 сентября в 10» он разбирал в 2110 год
    if not MIN_YEAR <= when.year <= MAX_YEAR:
        return None

    rest = (text[:start] + " " + text[end:]).strip(" ,.;—-:")
    rest = re.sub(r"\s{2,}", " ", rest)
    return Parsed(when=when, text=rest, fragment=fragment)


def as_due(when: datetime, now_local: datetime) -> tuple[datetime, bool]:
    """Разобранное «когда» → `(due_at, all_day)`.

    Время, которого никто не называл, `dateparser` берёт из «сейчас»: сказано
    «завтра» в 14:37 — получите «завтра в 14:37». Показать придуманное время
    хуже, чем показать один день: человек подтверждает карточку кнопкой и
    унесёт выдумку в базу. Признак «время не названо» — совпадение часа и
    минуты с текущими; цена — «через сутки» тоже станет записью на весь день,
    но это куда более редкая фраза, чем «завтра».

    Общая функция, а не копия в каждом хендлере: правило родилось в разборе
    без модели, а правку даты ответом на карточку завели позже и про него
    забыли — «завтра» там давало «завтра в 14:37, не на весь день».
    """
    invented = (when.hour, when.minute) == (now_local.hour, now_local.minute)
    all_day = invented or when.time() == time(0, 0)
    if not all_day:
        return when, False
    return when.replace(hour=0, minute=0, second=0, microsecond=0), True
