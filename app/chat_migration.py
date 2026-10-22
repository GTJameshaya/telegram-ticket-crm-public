"""Обработка перевода группы в супергруппу (смена chat_id).

Telegram меняет ID чата, когда группа становится супергруппой. Middleware ловит
ошибку TelegramMigrateToChat, обновляет ID чата в БД и повторяет запрос с новым ID.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from sqlalchemy import update

from app.db import get_sessionmaker
from app.models import Chat

log = logging.getLogger(__name__)


async def migrate_chat_id(old_id: int, new_id: int) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(update(Chat).where(Chat.tg_chat_id == old_id).values(tg_chat_id=new_id))
        await session.commit()
    log.info("Чат %s переведён в супергруппу %s, ID обновлён", old_id, new_id)


class ChatMigrationMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]):
        try:
            return await make_request(bot, method)
        except TelegramMigrateToChat as e:
            old_id = getattr(method, "chat_id", None)
            if old_id is None:
                raise
            await migrate_chat_id(int(old_id), e.migrate_to_chat_id)
            method.chat_id = e.migrate_to_chat_id
            return await make_request(bot, method)


def install(bot: Bot) -> Bot:
    bot.session.middleware(ChatMigrationMiddleware())
    return bot
