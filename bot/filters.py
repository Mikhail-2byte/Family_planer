import re

from aiogram import Bot, F
from aiogram.filters import BaseFilter
from aiogram.types import Message

# Бот живёт только в общем семейном чате — в личке семьи нет
IN_GROUP = F.chat.type.in_({"group", "supergroup"})

# То же для колбэков: у CallbackQuery чат лежит внутри сообщения
IN_GROUP_CB = F.message.chat.type.in_({"group", "supergroup"})

# Личка: семьи там нет, поэтому вся работа сводится к отказу.
# Аналог для колбэков не нужен — кнопок бот в личку не шлёт
IN_PRIVATE = F.chat.type == "private"

# Префикс, которым человек явно просит разобрать фразу
TRIGGER_PREFIX = "+"


class IsTrigger(BaseFilter):
    """Сообщение адресовано боту — значит его можно вести в LLM (шаг 3a.5).

    Режим `trigger` из `PLAN.md`: обращением считается префикс `+`, ответ на
    сообщение бота или упоминание `@bot`. Всё остальное — обычная переписка
    семьи, и она **не должна порождать ни одного вызова модели**: это и деньги,
    и отправка чужих разговоров в сторонний API.

    Отдаёт хендлеру `payload` — текст без самого признака обращения, иначе
    модель будет разбирать «+» и «@бот» как часть фразы.
    """

    async def __call__(self, message: Message, bot: Bot) -> dict | bool:
        text = (message.text or "").strip()
        if not text:
            return False

        if text.startswith(TRIGGER_PREFIX):
            return self._payload(text[len(TRIGGER_PREFIX) :])

        me = await bot.me()  # aiogram кеширует, в сеть на каждое сообщение не ходит
        reply = message.reply_to_message
        if reply is not None and reply.from_user is not None and reply.from_user.id == me.id:
            return self._payload(text)

        mention = f"@{me.username}"
        if me.username and mention.lower() in text.lower():
            # Вырезаем упоминание без учёта регистра: Telegram его сохраняет,
            # а человек пишет как придётся
            cut = re.sub(re.escape(mention), " ", text, flags=re.IGNORECASE)
            return self._payload(cut)

        return False

    @staticmethod
    def _payload(text: str) -> dict | bool:
        cleaned = " ".join(text.split())
        # Пустое обращение разбирать нечего: «+» или голое «@бот» — не фраза
        return {"payload": cleaned} if cleaned else False
