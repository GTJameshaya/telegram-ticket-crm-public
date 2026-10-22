"""Общие действия с Telegram: карточки в CRM-чате, уведомления, публикация ответов клиенту.

Используется и ботом, и веб-панелью, и mail-worker (у каждого свой экземпляр aiogram.Bot).
"""
import json
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Kind, Message, Status, Ticket
from app.services import tickets as svc

log = logging.getLogger(__name__)

MAX_TEXT = 3000


def _cut(text: str) -> str:
    text = text or ""
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def ticket_header(ticket: Ticket) -> str:
    category = ticket.category.name if ticket.category else "—"
    author = ticket.author.name if ticket.author else "—"
    lines = [
        f"<b>Новое сообщение в тикете #{ticket.number}</b>",
        f"Категория: {escape(category)} · Приоритет: {ticket.priority_label} · Статус: {ticket.status_label}",
        f"Автор: {escape(author)}",
    ]
    if ticket.assignee:
        lines.append(f"Исполнитель: {escape(ticket.assignee.name)}")
    return "\n".join(lines)


def meta_text(meta: str) -> str:
    """Метаданные внешнего источника (JSON-строка) → текст «ключ: значение» построчно (без HTML)."""
    if not meta:
        return ""
    try:
        data = json.loads(meta)
    except ValueError:
        return meta
    if isinstance(data, dict):
        return "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
                         for k, v in data.items())
    return json.dumps(data, ensure_ascii=False)


def meta_lines(meta: str) -> list[str]:
    text = meta_text(meta)
    return [escape(_cut(text))] if text else []


def card_text(ticket: Ticket, text: str, meta: str = "") -> str:
    body = f"\n\n«{escape(_cut(text))}»" if text else ""
    extra = meta_lines(meta)
    return ticket_header(ticket) + body + ("\n\n" + "\n".join(extra) if extra else "")


def card_keyboard(ticket: Ticket) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Взять в работу", callback_data=f"st:{ticket.id}:{Status.IN_PROGRESS}"),
        InlineKeyboardButton(text="Отложить", callback_data=f"st:{ticket.id}:{Status.POSTPONED}"),
        InlineKeyboardButton(text="Закрыть", callback_data=f"st:{ticket.id}:{Status.CLOSED}"),
    ]])


def publish_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить клиенту", callback_data=f"pub:{msg_id}"),
    ]])


async def _support_chat_id(session: AsyncSession) -> int | None:
    chat = await svc.get_support_chat(session)
    if chat is None:
        log.warning("CRM-чат поддержки не назначен (/set_support_chat)")
        return None
    return chat.tg_chat_id


def _reply_to(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


async def copy_to_support(bot: Bot, session: AsyncSession, ticket: Ticket, from_chat_id: int,
                          message_id: int) -> None:
    """Пересылает (копирует) сообщение с вложениями в CRM-чат реплаем на карточку."""
    support_id = await _support_chat_id(session)
    if support_id is None:
        return
    try:
        copied = await bot.copy_message(chat_id=support_id, from_chat_id=from_chat_id, message_id=message_id,
                                        reply_parameters=_reply_to(ticket.card_message_id))
        await svc.add_link(session, support_id, copied.message_id, ticket.id)
    except TelegramBadRequest as e:
        log.warning("Не удалось скопировать вложение: %s", e)


async def post_card(bot: Bot, session: AsyncSession, ticket: Ticket, text: str, meta: str = "") -> None:
    """Отправляет карточку нового тикета в CRM-чат."""
    support_id = await _support_chat_id(session)
    if support_id is None:
        return
    try:
        sent = await bot.send_message(support_id, card_text(ticket, text, meta), parse_mode="HTML",
                                      reply_markup=card_keyboard(ticket))
    except Exception as e:  # тикет не должен теряться из-за ошибки отправки карточки
        log.error("Не удалось отправить карточку #%s в CRM-чат: %s", ticket.number, e)
        return
    ticket.card_message_id = sent.message_id
    await svc.add_link(session, support_id, sent.message_id, ticket.id)


async def refresh_card(bot: Bot, session: AsyncSession, ticket: Ticket) -> None:
    """Обновляет карточку (статус/приоритет/исполнитель) редактированием, без новых сообщений."""
    support_id = await _support_chat_id(session)
    if support_id is None or not ticket.card_message_id:
        return
    full = await svc.get_ticket(session, ticket.id)
    first_meta = full.messages[0].meta if full.messages else ""
    try:
        await bot.edit_message_text(card_text(full, svc.first_text(full), first_meta), chat_id=support_id,
                                    message_id=ticket.card_message_id, parse_mode="HTML",
                                    reply_markup=card_keyboard(full))
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("Не удалось обновить карточку #%s: %s", ticket.number, e)


async def notify_new_message(bot: Bot, session: AsyncSession, ticket: Ticket, msg: Message,
                             reopened: bool = False, copy_from: tuple[int, int] | None = None) -> None:
    """«Новое сообщение в тикете #N» в CRM-чат (реплаем на карточку) + обновление карточки."""
    support_id = await _support_chat_id(session)
    if support_id is None:
        return
    who = msg.author.name if msg.author else ""
    prefix = "📧 Письмо" if msg.kind == Kind.EMAIL_IN else (f"От: {escape(who)}" if who else "")
    parts = [f"<b>Новое сообщение в тикете #{ticket.number}</b>"]
    if reopened:
        parts.append("♻️ Тикет переоткрыт → В работе")
    if prefix:
        parts.append(prefix)
    if msg.text:
        parts.append(f"«{escape(_cut(msg.text))}»")
    parts.extend(meta_lines(msg.meta))
    sent = await bot.send_message(support_id, "\n".join(parts), parse_mode="HTML",
                                  reply_parameters=_reply_to(ticket.card_message_id),
                                  reply_markup=card_keyboard(ticket))
    await svc.add_link(session, support_id, sent.message_id, ticket.id)
    if copy_from and msg.attachments:
        await copy_to_support(bot, session, ticket, *copy_from)
    await refresh_card(bot, session, ticket)


async def publish_to_client(bot: Bot, session: AsyncSession, ticket: Ticket, msg: Message) -> None:
    """Публикует ответ оператора в исходный рабочий чат реплаем на сообщение автора."""
    if not ticket.source_chat_id:
        raise RuntimeError("У тикета нет исходного чата")
    sent = await bot.send_message(
        ticket.source_chat_id,
        f"Ответ по тикету #{ticket.number}:\n{msg.text}",
        reply_parameters=_reply_to(ticket.source_message_id),
    )
    await svc.mark_published(session, msg, ticket.source_chat_id, sent.message_id)
