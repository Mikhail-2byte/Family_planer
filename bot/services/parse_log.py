"""Лог разбора свободного текста — `parse.log` (шаг 3b.7).

Зачем файл, а не таблица: смотреть в него будут глазами и раз в месяц —
«где модель ошибается» и «сколько вызовов в сутки». Отдельная таблица с
миграцией ради этого не окупается.

На разбор пишутся две строки: сам разбор и вердикт человека
(`saved` / `cancelled` / `edited`) — по ним и считается 3b.8. Строки связаны
`card` — `message_id` карточки: он же ключ черновика.

Токенов и цены тут нет намеренно. Точную стоимость отдаёт страница Activity в
OpenRouter, а наш подсчёт по токенам был бы оценкой — ровно тем, что пункт 3b.8
и запрещает.
"""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from bot.config import settings

log = logging.getLogger(__name__)

# Рядом с БД, а не в корне: в Docker `./data` смонтирована томом, и только
# оттуда лог переживёт пересборку образа
PATH = settings.db_path.parent / "parse.log"

# Ротация: 2 МБ — это примерно двадцать тысяч строк разбора, то есть годы
# работы семейного чата. Потолок нужен не ради места на диске, а ради того,
# что в файле лежит: полные фразы людей и полные ответы модели
MAX_BYTES = 2 * 1024 * 1024


# Счётчик обращений к модели за сегодняшние сутки. Держится в памяти, а
# заполняется из файла один раз — при первом обращении после старта.
#
# Зачем вообще: у бесплатного тира OpenRouter лимит **на аккаунт**, а не на
# ключ, и именно из-за него отменён режим `all`. Учёта при этом не было
# никакого — исчерпав лимит, бот молча уходил на `dateparser`, и разбор
# незаметно становился хуже. Человек видел «разобрал без ИИ» и думал, что
# сломалась сеть.
#
# Сутки считаем по UTC: там же проходит граница у самого OpenRouter, и `at`
# в логе пишется в UTC. Таймзона семьи тут ни при чём — лимит не её.
_counted_day: date | None = None
_counted = 0


def _today() -> date:
    return datetime.now(UTC).date()


def write(**record: Any) -> None:
    """Дописать строку JSON. Исключений наружу не выпускает.

    Лог — побочная вещь: упавшая запись в него не должна стоить человеку
    карточки. Отсюда `except Exception` вокруг всего, включая открытие файла.
    """
    record["at"] = datetime.now(UTC).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )
    if _is_llm_call(record):
        _bump()
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate()
        with PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("Не удалось записать %s", PATH)


def _rotate() -> None:
    """Переложить разросшийся лог в `.1`, освободив имя под новый.

    Одно поколение, а не пять, и это осознанно: в файл уходит **полный текст
    человека и полный ответ модели**. Он нужен, чтобы подкручивать промпт и
    считать вызовы, но хранить семейную переписку годами ради этого незачем —
    ровно тот же довод, по которому ежедневный бэкап не уходит в облако.

    `replace`, а не «дописать и обрезать»: переименование атомарно, и
    оборвавшись на нём, мы теряем в худшем случае одно поколение, а не файл.
    Счётчик `calls_today` при этом переживает ротацию — он в памяти, и заново
    читать файл ему не придётся до полуночи.
    """
    if not PATH.exists() or PATH.stat().st_size < MAX_BYTES:
        return
    PATH.replace(PATH.with_suffix(PATH.suffix + ".1"))
    log.info("parse.log перевалил %s байт — переложен в .1", MAX_BYTES)


def _is_llm_call(record: dict[str, Any]) -> bool:
    """Строка, стоившая вызова модели.

    Голос (`event=voice`) сюда не входит: он уходит в Groq, у которого своя
    квота, — ровно ради этого он и отделён от OpenRouter.
    """
    return record.get("event") == "parse" and record.get("via") == "llm"


def _bump() -> None:
    global _counted_day, _counted
    today = _today()
    if _counted_day != today:
        _counted_day, _counted = today, 0
    _counted += 1


def calls_today() -> int:
    """Сколько раз сегодня обращались к модели.

    Первый вызов после старта читает файл, дальше счётчик живёт в памяти.
    Читать на каждое обращение было бы честнее при нескольких процессах, но
    процесс здесь один, а лог лежит на диске домашнего ПК и растёт.

    Сбой чтения — это `0`, а не исключение: счётчик существует ради подсказки,
    и уронить из-за него разбор было бы хуже, чем ошибиться в подсказке.
    """
    global _counted_day, _counted
    today = _today()
    if _counted_day == today:
        return _counted

    _counted_day, _counted = today, 0
    stamp = today.isoformat()
    try:
        with PATH.open(encoding="utf-8") as fh:
            for line in fh:
                # Дешёвая отсечка до разбора JSON: строк за прошлые дни в файле
                # заведомо больше, чем за сегодня
                if stamp not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if _is_llm_call(record) and str(record.get("at", "")).startswith(stamp):
                    _counted += 1
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("Не удалось пересчитать %s", PATH, exc_info=True)
    return _counted
