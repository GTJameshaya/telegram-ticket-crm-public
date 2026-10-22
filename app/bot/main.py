"""Точка входа бота: python -m app.bot.main"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from app.chat_migration import install as install_migration
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from app.bot.handlers import router
from app.config import get_settings
from app.db import get_sessionmaker
from app.services.tickets import ensure_admins

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN не задан")

    async with get_sessionmaker()() as session:
        await ensure_admins(session, settings.admin_tg_ids)

    bot = install_migration(Bot(settings.bot_token))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="help", description="Справка"),
        BotCommand(command="t", description="Быстрый ответ по шаблону"),
        BotCommand(command="priority", description="Сменить приоритет тикета"),
        BotCommand(command="register_chat", description="Зарегистрировать рабочий чат"),
        BotCommand(command="set_support_chat", description="Назначить CRM-чат"),
        BotCommand(command="id", description="Показать ID чата"),
    ])
    if settings.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="CRM", web_app=WebAppInfo(url=settings.webapp_url))
        )

    log.info("Бот запущен")
    # Обрабатываем апдейты последовательно: нагрузка маленькая, а так нет гонок (например, у альбомов).
    await dp.start_polling(bot, handle_as_tasks=False,
                           allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
