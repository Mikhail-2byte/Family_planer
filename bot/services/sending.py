"""Отправка в чат семьи и политика ошибок Telegram.

Отдельный модуль, потому что политика нужна и тикеру, и дайджесту (а на этапе
2.7 — ещё и панели). Держать её в `ticker.py` значило бы заставить `digest.py`
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


async def deliver(bot: Bot, family: Family, text: str) -> str:
    """Отправить сообщение в чат семьи. Исключений наружу не выпускает."""
    try:
        await bot.send_message(family.chat_id, text)
        return OK
    except TelegramForbiddenError:
        log.warning("Нет доступа в чат %s", family.chat_id)
        return FORBIDDEN
    except TelegramBadRequest as exc:
        # Сломанная разметка или превышение 4096 символов. Ретрай даст ровно то
        # же самое, поэтому сдаёмся сразу — иначе сообщение будет вечно
        # переотправляться на каждом тике
        log.error("Чат %s не принял сообщение: %s", family.chat_id, exc.message)
        return BROKEN
    except TelegramRetryAfter as exc:
        log.warning("Флуд-контроль в чате %s, ждём %s с", family.chat_id, exc.retry_after)
        return RETRY
    except Exception:
        log.exception("Не удалось отправить в чат %s", family.chat_id)
        return RETRY
