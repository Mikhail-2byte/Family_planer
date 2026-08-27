"""Регистрация функции `lower_unicode` во всех SQLite-соединениях.

Встроенный `lower()` в SQLite умеет только ASCII: «Отпуск» он оставит как есть,
и поиск по «отпуск» ничего не найдёт. ICU-расширения в стандартной сборке нет,
поэтому отдаём складывание регистра Python — он знает про кириллицу.

Слушатель повешен на класс Engine, а не на конкретный движок: так он работает
и в боте, и в тестах, где движок создаётся свой.
"""

from sqlalchemy import event
from sqlalchemy.engine import Engine


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


@event.listens_for(Engine, "connect")
def _register_lower_unicode(dbapi_connection, _record) -> None:
    dbapi_connection.create_function("lower_unicode", 1, _lower, deterministic=True)
