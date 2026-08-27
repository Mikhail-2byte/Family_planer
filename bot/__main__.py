import asyncio
import logging
import sys
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from bot.config import settings
from bot.db.session import engine
from bot.handlers import routers
from bot.middlewares import FamilyMiddleware
from bot.services import ticker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


def make_bot() -> Bot:
    # Пустой TELEGRAM_PROXY = ходим напрямую. Прокси нужен из-за нестабильной
    # доступности api.telegram.org из России (PLAN.md, «Сеть и деплой»)
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


async def _stop_ticker(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    # CancelledError здесь ожидаем — это ответ на нашу же отмену
    with suppress(asyncio.CancelledError):
        await task


async def main() -> None:
    if not settings.bot_token:
        log.error("BOT_TOKEN пуст. Заполните его в .env — токен даёт @BotFather.")
        sys.exit(1)

    bot = make_bot()
    dp = Dispatcher()
    dp.update.outer_middleware(FamilyMiddleware())
    for router in routers:
        dp.include_router(router)

    task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        log.info("Запуск @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=True)

        task = asyncio.create_task(ticker.run(bot), name="ticker")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # Порядок обязателен: сначала перестаём порождать отправки, потом
        # закрываем сеть, последним — движок БД. Каждый шаг в своём try,
        # чтобы падение одного не съело остальные
        for step in (_stop_ticker(task), bot.session.close(), engine.dispose()):
            try:
                await step
            except Exception:
                log.exception("Ошибка при остановке")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # SystemExit намеренно не ловим: код возврата должен дойти до
        # Планировщика задач, иначе ошибка конфигурации выглядит как успех
        log.info("Остановлен")
