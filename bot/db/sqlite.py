"""Настройка каждого SQLite-соединения: `lower_unicode` и режим журнала.

Встроенный `lower()` в SQLite умеет только ASCII: «Отпуск» он оставит как есть,
и поиск по «отпуск» ничего не найдёт. ICU-расширения в стандартной сборке нет,
поэтому отдаём складывание регистра Python — он знает про кириллицу.

Слушатель повешен на класс Engine, а не на конкретный движок: так он работает
и в боте, и в тестах, где движок создаётся свой.
"""

import logging

from sqlalchemy import event
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

# Сколько ждать освобождения блокировки, прежде чем сдаться (мс).
# С этапа 2 в базу пишут одновременно хендлеры и фоновый тикер.
BUSY_TIMEOUT_MS = 5000


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


@event.listens_for(Engine, "connect")
def _setup_connection(dbapi_connection, _record) -> None:
    dbapi_connection.create_function("lower_unicode", 1, _lower, deterministic=True)

    cursor = dbapi_connection.cursor()
    try:
        # WAL разводит писателя и читателей: без него тикер, отправляющий
        # напоминание, блокирует хендлер, отвечающий на команду.
        # На базе в памяти (тесты) SQLite молча остаётся в режиме `memory`.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except Exception:  # pragma: no cover — не должно мешать боту стартовать
        log.warning("Не удалось выставить PRAGMA для SQLite", exc_info=True)
    finally:
        cursor.close()
