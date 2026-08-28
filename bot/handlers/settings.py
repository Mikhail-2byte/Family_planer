"""`/settings` — таймзона и время утренней сводки (3b.6).

Режима прослушивания на экране нет: второй режим (`all`, шаг 3b.7) отменён
28.08.2026, у бота он один — отзываться только на обращение. Колонка
`families.listen_mode` осталась в базе мёртвой, см. комментарий в `db/models.py`.
Что именно бот считает обращением, экран всё же говорит (`SETTINGS_MODE_NOTE`):
молчание на обычную реплику иначе читается как поломка.

Механизм правки тот же, что у карточки разбора: тап по кнопке, ответ реплаем на
сообщение бота. Диалог не FSM — по той же причине, что и там: ключ FSM «чат +
пользователь», а настройки у семьи общие, и ответить на вопрос может не тот, кто
нажал кнопку.
"""

import logging
from functools import lru_cache
from zoneinfo import available_timezones

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.db import repo
from bot.db.models import Family
from bot.filters import IN_GROUP, IN_GROUP_CB
from bot.services import sending
from bot.services import timeutil as tu

router = Router()
router.message.filter(IN_GROUP)
router.callback_query.filter(IN_GROUP_CB)

log = logging.getLogger(__name__)

TZ = "set:tz"
DIGEST = "set:digest"

KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🌍 Таймзона", callback_data=TZ),
            InlineKeyboardButton(text="☀️ Время сводки", callback_data=DIGEST),
        ]
    ]
)

# Ключ — (chat_id, message_id) сообщения с настройками, значение — что правим.
# Живёт в памяти и умирает с перезапуском, как черновики разбора
_pending: dict[tuple[int, int], str] = {}
MAX_PENDING = 20


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    """Все зоны базы IANA. Считается один раз за жизнь процесса."""
    return frozenset(available_timezones())


def _awaits(message: Message) -> bool:
    reply = message.reply_to_message
    return reply is not None and (message.chat.id, reply.message_id) in _pending


def _card(family: Family) -> str:
    return texts.settings_card(family.tz, family.digest_time)


@router.message(Command("settings"))
async def cmd_settings(message: Message, family: Family) -> None:
    await message.answer(_card(family), reply_markup=KEYBOARD)


@router.callback_query(F.data.in_({TZ, DIGEST}))
async def ask(call: CallbackQuery) -> None:
    key = (call.message.chat.id, call.message.message_id)
    if len(_pending) >= MAX_PENDING and key not in _pending:
        _pending.pop(next(iter(_pending)))
    _pending[key] = call.data
    prompt = texts.SETTINGS_ASK_TZ if call.data == TZ else texts.SETTINGS_ASK_DIGEST
    await call.answer(prompt, show_alert=True)


@router.message(_awaits)
async def take_value(
    message: Message, session: AsyncSession, family: Family, bot: Bot
) -> None:
    key = (message.chat.id, message.reply_to_message.message_id)
    field = _pending.pop(key)
    raw = (message.text or "").strip()

    if field == TZ:
        # Сверка со списком зон, а не `ZoneInfo(raw)` в try. Конструктор
        # ненадёжен как валидатор с двух сторон: на строке с переводом строки
        # внутри он бросает `OSError` мимо любого разумного `except` — а
        # упавший хендлер означает апдейт, потерянный навсегда; и он же молча
        # принимает `Europe\Moscow` на Windows, хотя то же значение на
        # Linux-VPS сломало бы рендер каждой даты. Битая зона в базе валит
        # разом тикер, дайджест и панель
        if raw not in _known_zones():
            _pending[key] = field
            await message.reply(texts.SETTINGS_BAD_TZ)
            return
        await repo.set_family_settings(session, family, tz=raw)
    else:
        try:
            moment = tu.parse_hhmm(raw)
        except ValueError:
            _pending[key] = field
            await message.reply(texts.SETTINGS_BAD_TIME)
            return
        await repo.set_family_settings(session, family, digest_time=f"{moment:%H:%M}")

    await message.reply(texts.SETTINGS_SAVED)
    # Экран настроек перерисовывается на месте: иначе в чате остаётся сообщение
    # со старыми значениями, и в следующий раз человек нажмёт кнопку на нём
    await sending.edit(bot, family, key[1], _card(family), KEYBOARD)
