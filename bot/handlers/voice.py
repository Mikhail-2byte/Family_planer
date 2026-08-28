"""Голосовые сообщения: кнопка-приглашение → расшифровка → тот же разбор (этап 5).

Голосовому недоступен ни один признак обращения из `filters.IsTrigger`: текста
у него нет, а подписи к голосовым Telegram не даёт — остаётся только реплай.
Поэтому обращением тут служит **кнопка «🎤 Голосом»**: тап ставит приглашение,
и ближайшее голосовое от того же человека уходит в расшифровку. Голосовое без
тапа не обрабатывается вовсе — ни одного внешнего вызова, ни одной строки
семейной болтовни в чужом API.

Приглашение живёт в модульном словаре, а не в FSM. Ключ FSM «чат + пользователь»
здесь как раз подошёл бы (говорит тот, кто нажал), но состояние мастера `/new` —
соседняя машина, и мешать их незачем; словарь к тому же даёт естественную
проверку срока.

**Роутер обязан стоять раньше мастера.** Шаги `/new` ловят `F.text`, и «🎤
Голосом», нажатое посреди мастера, стало бы заголовком записи. Само голосовое
мастеру не мешает: все его шаги под `F.text`.

Расшифрованный текст показывается человеку **до** разбора: так видно, что
именно бот услышал, и ошибка распознавания не превращается молча в
неправильную задачу.
"""

import logging
from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards as kb
from bot import texts
from bot.config import settings
from bot.db.models import Family, Member
from bot.filters import IN_GROUP
from bot.handlers import capture
from bot.services import parse_log
from bot.services import timeutil as tu
from bot.services import voice as stt

router = Router()
router.message.filter(IN_GROUP)

log = logging.getLogger(__name__)

# Ключ — (chat_id, user_id), значение — до какого момента ждём голосовое.
# Вытеснения нет и не нужно: повторный тап перезаписывает свою же запись, а
# ключей столько же, сколько людей в чатах бота — у этого бота чат один.
# Тем и отличается от `capture._drafts`, где карточки копятся по одной на фразу
# и потому есть `MAX_DRAFTS`
_awaiting: dict[tuple[int, int], datetime] = {}


def _invited(message: Message) -> bool:
    """Ждём ли голосовое именно от этого человека и не истёк ли срок?

    Фильтр, а не проверка внутри хендлера: иначе хендлер съедал бы **все**
    голосовые чата, и реплай голосом на карточку правки перестал бы доходить
    до `capture.edit_field`.
    """
    if message.from_user is None:
        return False
    deadline = _awaiting.get((message.chat.id, message.from_user.id))
    return deadline is not None and tu.now_utc() < deadline


# `F.from_user` — не украшение: в группе, привязанной к каналу, сообщение
# приходит от имени канала и автора у него нет. Упавший хендлер стоит апдейта:
# offset Telegram сдвигается независимо от исхода
@router.message(F.text == kb.BTN_VOICE, F.from_user)
async def invite(message: Message) -> None:
    """Тап по кнопке: «слушаю» на одно сообщение и на несколько минут."""
    if not settings.stt_key:
        # Без ключа голос выключен целиком — обещать «слушаю» нельзя
        await message.answer(texts.VOICE_OFF, reply_markup=kb.main_keyboard())
        return

    window = timedelta(seconds=settings.voice_window_seconds)
    _awaiting[(message.chat.id, message.from_user.id)] = tu.now_utc() + window
    await message.answer(texts.VOICE_ASK, reply_markup=kb.main_keyboard())


@router.message(F.voice, _invited)
async def dictate(
    message: Message,
    session: AsyncSession,
    family: Family,
    member: Member,
    bot: Bot,
) -> None:
    """Голосовое после приглашения: скачать → расшифровать → в общий разбор."""
    # Приглашение снимается **до** первой же паузы на await, а не после успеха.
    # aiogram обрабатывает апдейты параллельно, и два голосовых подряд успели бы
    # оба пройти `_invited`: один тап — два вызова расшифровки, два вызова
    # модели и две карточки на одну фразу. Тот же приём, что в `capture._finish`.
    # На неудаче приглашение возвращается со **своим прежним сроком**: осечка не
    # должна стоить второго тапа, но и продлевать окно ей незачем
    key = (message.chat.id, message.from_user.id)
    deadline = _awaiting.pop(key, None)
    if deadline is None:
        return  # приглашение уже забрал параллельный апдейт

    if message.voice.duration > settings.voice_max_seconds:
        # До скачивания и до сети: длинную запись не за что качать
        _awaiting[key] = deadline
        await message.reply(texts.voice_too_long(settings.voice_max_seconds))
        return

    buffer = BytesIO()
    try:
        # Скачивание идёт сессией бота, а у неё уже настроен TELEGRAM_PROXY
        await bot.download(message.voice, destination=buffer)
    except Exception:
        log.warning("Не удалось скачать голосовое", exc_info=True)
        _awaiting[key] = deadline
        await message.reply(texts.VOICE_FAILED)
        return

    # Файл уходит как есть: Whisper-совместимый эндпоинт принимает ogg/opus,
    # поэтому конвертации (и ffmpeg) в проекте нет
    text = await stt.transcribe(buffer.getvalue())
    parse_log.write(
        event="voice",
        chat=message.chat.id,
        model=settings.stt_model,
        duration=message.voice.duration,
        chars=len(text or ""),
    )
    if text is None:
        _awaiting[key] = deadline  # ещё одна попытка без второго тапа
        await message.reply(texts.VOICE_FAILED)
        return

    await message.reply(texts.voice_heard(text))
    await capture.handle_phrase(message, text, session, family, member, bot)
