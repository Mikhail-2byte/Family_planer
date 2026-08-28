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
from datetime import datetime, timezone

from bot.config import settings

log = logging.getLogger(__name__)

# Рядом с БД, а не в корне: в Docker `./data` смонтирована томом, и только
# оттуда лог переживёт пересборку образа
PATH = settings.db_path.parent / "parse.log"


def write(**record) -> None:
    """Дописать строку JSON. Исключений наружу не выпускает.

    Лог — побочная вещь: упавшая запись в него не должна стоить человеку
    карточки. Отсюда `except Exception` вокруг всего, включая открытие файла.
    """
    record["at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        timespec="seconds"
    )
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("Не удалось записать %s", PATH)
