"""Отправка в чат семьи и политика ошибок Telegram.

Отдельный модуль, потому что политика нужна тикеру, дайджесту и панели
(этап 2п). Держать её в `ticker.py` значило бы заставить `digest.py`
импортировать тикер, который сам импортирует дайджест.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.db.models import Family

log = logging.getLogger(__name__)

OK = "ok"
FORBIDDEN = "forbidden"  # бота выгнали — ретраить бессмысленно
BROKEN = "broken"  # наш текст не принимают: повтор даст ту же ошибку
RETRY = "retry"  # сеть или флуд-контроль — попробуем на следующем тике
NOT_FOUND = "not_found"  # сообщения больше нет — править нечего, нужно новое


async def send(bot: Bot, family: Family, text: str, *, silent: bool = False) -> tuple[str, int | None]:
    """Отправить сообщение в чат семьи. Исключений наружу не выпускает.

    Отдаёт ещё и `message_id`: панели он нужен, чтобы потом её редактировать.
    """
    try:
        message = await bot.send_message(
            family.chat_id, text, disable_notification=silent
        )
        return OK, message.message_id
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN, None
    except TelegramBadRequest as exc:
        # Сломанная разметка или превышение 4096 символов. Ретрай даст ровно то
        # же самое, поэтому сдаёмся сразу — иначе сообщение будет вечно
        # переотправляться на каждом тике
        log.error("Чат %s не принял сообщение: %s", family.chat_id, exc.message)
        return BROKEN, None
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY, None
    except Exception:
        log.exception("Не удалось отправить в чат %s", family.chat_id)
        return RETRY, None


async def deliver(bot: Bot, family: Family, text: str) -> str:
    """То же, что `send`, но без `message_id` — тикеру и дайджесту он не нужен."""
    status, _ = await send(bot, family, text)
    return status


async def edit(bot: Bot, family: Family, message_id: int, text: str) -> str:
    """Перерисовать сообщение в чате семьи. Исключений наружу не выпускает.

    Фоновый двойник `views.edit_or_ignore`: тот работает от `CallbackQuery` и
    обязан пропускать наружу неожиданные ошибки, чтобы они дошли до нажавшего.
    Здесь наоборот — видимых ошибок быть не должно, а вместо исключения
    возвращается статус, по которому панель решает, править дальше или
    выпускать новую.
    """
    try:
        await bot.edit_message_text(text, chat_id=family.chat_id, message_id=message_id)
        return OK
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN
    except TelegramBadRequest as exc:
        # Сравнивать надо `exc.message`: в тексте исключения есть и метод, и описание
        if "message is not modified" in exc.message:
            # Панель уже показывает что нужно. Это норма, а не сбой: именно так
            # отсеиваются холостые перерисовки. Важно, что не BROKEN и не
            # NOT_FOUND — иначе каждая из них плодила бы новую панель
            return OK
        if "message to edit not found" in exc.message or "message can't be edited" in exc.message:
            return NOT_FOUND
        log.error("Чат %s не принял правку: %s", family.chat_id, exc.message)
        return BROKEN
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY
    except Exception:
        log.exception("Не удалось отредактировать в чате %s", family.chat_id)
        return RETRY
