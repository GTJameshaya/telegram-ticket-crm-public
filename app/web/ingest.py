"""HTTP API приёма тикетов от внешних систем (например, другого Telegram-бота).

Telegram не доставляет ботам сообщения других ботов, поэтому внешний бот после отправки сообщения в рабочий
чат дублирует его сюда: POST /api/ingest/ticket (multipart/form-data, Authorization: Bearer <INGEST_TOKEN>).
Поведение повторяет упоминание бота в чате: тикет, карточка в CRM-чате, «Создан тикет #N» в рабочем чате
и привязка сообщений, чтобы реплаи людей в этой ветке попадали в тикет.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import zlib

from aiogram import Bot
from aiogram.types import BufferedInputFile, ReplyParameters
from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Attachment, Chat
from app.services import tickets as svc
from app.telegram_ops import notify_new_message, post_card

log = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 20
DEFAULT_CATEGORY = "Внешний источник"
DEFAULT_AUTHOR = "API"
# Псевдо-пользователи API (автор по имени) живут вне диапазона Telegram ID
_API_USER_BASE = 9_000_000_000_000_000


def _check_token(request: Request) -> None:
    token = get_settings().ingest_token.strip()
    if not token:
        raise HTTPException(404, "Not Found")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), token.encode()):
        raise HTTPException(401, "Неверный или отсутствующий токен (Authorization: Bearer <INGEST_TOKEN>)",
                            headers={"WWW-Authenticate": "Bearer"})


def _str(form, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _int(form, name: str) -> int | None:
    value = _str(form, name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(422, f"Поле {name} должно быть целым числом")


def _meta(form) -> str:
    raw = _str(form, "meta")
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except ValueError:
        raise HTTPException(422, "Поле meta должно быть корректным JSON")


def strip_leading_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(rf"^\s*@{re.escape(bot_username)}\b[\s,:]*", "", text, flags=re.IGNORECASE).strip()


def _api_user_tg_id(name: str) -> int:
    return -(_API_USER_BASE + zlib.crc32(name.encode("utf-8")))


async def _read_files(form) -> list[tuple[str, bytes, str]]:
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Слишком много файлов (максимум {MAX_FILES})")
    out = []
    for f in files:
        data = await f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"Файл {f.filename!r} больше 20 МБ")
        if not data and not f.filename:
            continue  # пустое поле формы
        out.append((f.filename or "file", data, f.content_type or ""))
    return out


async def _chat_title(bot: Bot, chat_id: int) -> str:
    try:
        info = await bot.get_chat(chat_id)
        return (getattr(info, "title", None) or "").strip() or str(chat_id)
    except Exception as e:  # noqa: BLE001
        log.warning("ingest: не удалось получить название чата %s: %s", chat_id, e)
        return str(chat_id)


def _reply(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


async def _upload_files(bot: Bot, session, ticket, msg, support: Chat | None, files, warnings: list[str]) -> None:
    """Загружает файлы документами в CRM-чат (реплаем на карточку) и сохраняет их file_id во вложения."""
    if not files:
        return
    if support is None:
        warnings.append("CRM-чат не назначен — файлы не сохранены")
        return
    for name, data, mime in files:
        try:
            sent = await bot.send_document(support.tg_chat_id, BufferedInputFile(data, filename=name),
                                           reply_parameters=_reply(ticket.card_message_id))
        except Exception as e:  # noqa: BLE001
            log.warning("ingest: не удалось загрузить файл %s в тикет #%s: %s", name, ticket.number, e)
            warnings.append(f"Файл {name} не загружен: {e}")
            continue
        doc = getattr(sent, "document", None)
        if doc is None:
            warnings.append(f"Файл {name}: Telegram не вернул document")
            continue
        msg.attachments.append(Attachment(tg_file_id=doc.file_id, file_name=getattr(doc, "file_name", None) or name,
                                          mime=getattr(doc, "mime_type", None) or mime, file_type="document"))
        await svc.add_link(session, support.tg_chat_id, sent.message_id, ticket.id)
    await session.flush()


@router.post("/api/ingest/ticket")
async def ingest_ticket(request: Request):
    _check_token(request)  # до разбора тела — без токена файлы не принимаем
    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(503, "Бот не настроен (BOT_TOKEN)")

    form = await request.form(max_files=MAX_FILES + 1, max_fields=50)
    try:
        raw_text = form.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise HTTPException(422, "Поле text обязательно")
        chat_id = _int(form, "chat_id")
        source_message_id = _int(form, "source_message_id")
        reply_to_ticket = _int(form, "reply_to_ticket")
        category_name = _str(form, "category")
        author_name = _str(form, "author") or DEFAULT_AUTHOR
        meta = _meta(form)
        files = await _read_files(form)
    finally:
        await form.close()

    me = await bot.me()
    text = strip_leading_mention(raw_text, me.username)
    warnings: list[str] = []

    async with get_sessionmaker()() as session:
        support = await svc.get_support_chat(session)
        if chat_id is not None and support is not None and chat_id == support.tg_chat_id:
            raise HTTPException(422, "chat_id указывает на CRM-чат поддержки, нужен рабочий чат")
        author = await svc.get_or_create_user(session, _api_user_tg_id(author_name), author_name)
        link_source = chat_id is not None and source_message_id is not None

        # --- дополнение существующего тикета ---
        if reply_to_ticket is not None:
            ticket = await svc.get_ticket_by_number(session, reply_to_ticket)
            if ticket is None:
                raise HTTPException(404, f"Тикет #{reply_to_ticket} не найден")
            msg, reopened = await svc.add_client_message(
                session, ticket, author=author, text=text, meta=meta,
                tg_chat_id=chat_id if link_source else None,
                tg_message_id=source_message_id if link_source else None,
            )
            try:
                await notify_new_message(bot, session, ticket, msg, reopened=reopened)
            except Exception as e:  # noqa: BLE001
                log.warning("ingest: не удалось уведомить CRM-чат о тикете #%s: %s", ticket.number, e)
                warnings.append(f"Уведомление в CRM-чат не отправлено: {e}")
            await _upload_files(bot, session, ticket, msg, support, files, warnings)
            await session.commit()
            return _result(ticket, created=False, warnings=warnings)

        # --- новый тикет ---
        chat = None
        category = None
        if chat_id is not None:
            chat = await svc.get_chat(session, chat_id)
            if chat is None:
                # Неизвестный чат регистрируем автоматически, как при первом упоминании бота
                title = await _chat_title(bot, chat_id)
                chat = await svc.register_chat(session, chat_id, title, category_name or title)
            elif chat.category_id is None:
                raise HTTPException(409, "Этот чат отключён от CRM (админ: /register_chat <категория>)")
        else:
            category = await svc.get_or_create_category(session, category_name or DEFAULT_CATEGORY)

        ticket, msg = await svc.create_ticket(session, chat=chat, category=category, author=author, text=text,
                                              tg_message_id=source_message_id if link_source else None, meta=meta)
        await post_card(bot, session, ticket, text, meta=meta)
        if support is None:
            warnings.append("CRM-чат не назначен (/set_support_chat) — карточка не отправлена")
        await _upload_files(bot, session, ticket, msg, support, files, warnings)

        if chat is not None:
            try:
                reply = await bot.send_message(chat.tg_chat_id, f"Создан тикет #{ticket.number}",
                                               reply_parameters=_reply(source_message_id))
                await svc.add_link(session, chat.tg_chat_id, reply.message_id, ticket.id)
                if ticket.source_message_id is None:
                    # Ответы операторов уйдут реплаем на «Создан тикет #N»
                    ticket.source_message_id = reply.message_id
            except Exception as e:  # noqa: BLE001
                log.warning("ingest: не удалось написать «Создан тикет #%s» в чат %s: %s",
                            ticket.number, chat.tg_chat_id, e)
                warnings.append(f"Сообщение «Создан тикет» в чат не отправлено: {e}")
        await session.commit()
        log.info("ingest: создан тикет #%s (чат %s, автор %s)", ticket.number, chat_id, author_name)
        return _result(ticket, created=True, warnings=warnings)


def _result(ticket, *, created: bool, warnings: list[str]) -> dict:
    out = {"ticket_number": ticket.number, "ticket_id": ticket.id, "created": created}
    if warnings:
        out["warnings"] = warnings
    return out
