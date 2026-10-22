"""Обработчики бота: регистрация чатов, создание тикетов, привязка реплаев, работа операторов."""
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Attachment, Kind, Message as DbMessage, Priority, ReplyTemplate, Role, Status
from app.services import tickets as svc
from app.services.mail import render, ticket_context
from app.telegram_ops import (
    copy_to_support,
    notify_new_message,
    post_card,
    publish_keyboard,
    publish_to_client,
    refresh_card,
)
from app.bot.utils import display_name, extract_attachments, mentions_bot, message_text, strip_mention

log = logging.getLogger(__name__)
router = Router()

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

# Альбомы (media group) приходят отдельными сообщениями; подпись с упоминанием — только у одного.
# Запоминаем media_group_id -> (ticket_id, db_message_id), чтобы приложить остальные файлы.
_media_groups: dict[str, tuple[int, int]] = {}
_MEDIA_GROUPS_LIMIT = 200

HELP = (
    "Я бот CRM тикетов.\n\n"
    "• В рабочем чате упомяните меня (@{username}) с текстом/файлами — создастся тикет.\n"
    "• Ответ (reply) на сообщение тикета добавляется в этот тикет.\n\n"
    "Команды админа:\n"
    "/register_chat &lt;категория&gt; — зарегистрировать рабочий чат\n"
    "/unregister_chat — отключить рабочий чат\n"
    "/set_support_chat — назначить этот чат CRM-чатом поддержки\n"
    "/add_operator &lt;tg_id&gt; [имя] — добавить оператора (или реплаем на сообщение)\n"
    "/remove_operator &lt;tg_id&gt; — убрать оператора\n\n"
    "Команды операторов (в CRM-чате, реплаем на карточку):\n"
    "Реплай на карточку — внутренний комментарий\n"
    "/t &lt;имя&gt; — быстрый ответ по шаблону (/t — список шаблонов)\n"
    "/priority &lt;Низкий|Обычный|Высокий|Срочный&gt; — сменить приоритет\n"
    "/id — показать ID чата и ваш Telegram ID"
)


def _remember_group(group_id: str, ticket_id: int, msg_id: int) -> None:
    if len(_media_groups) > _MEDIA_GROUPS_LIMIT:
        _media_groups.clear()
    _media_groups[group_id] = (ticket_id, msg_id)


async def _require_admin(message: Message, session) -> bool:
    user = await svc.get_user_by_tg(session, message.from_user.id)
    if user is None or not user.is_admin:
        await message.reply("Команда доступна только админам.")
        return False
    return True


# ---------- Общие команды ----------

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
@router.message(Command("help"))
async def cmd_start(message: Message, bot: Bot) -> None:
    me = await bot.me()
    markup = None
    settings = get_settings()
    if settings.webapp_url and message.chat.type == ChatType.PRIVATE:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=settings.webapp_url))
        ]])
    await message.answer(HELP.format(username=me.username), parse_mode="HTML", reply_markup=markup)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.reply(f"ID чата: <code>{message.chat.id}</code>\nВаш Telegram ID: "
                        f"<code>{message.from_user.id}</code>", parse_mode="HTML")


# ---------- Админ: чаты и операторы ----------

@router.message(Command("register_chat"), GROUPS)
async def cmd_register_chat(message: Message, command: CommandObject) -> None:
    async with get_sessionmaker()() as session:
        if not await _require_admin(message, session):
            return
        category = (command.args or "").strip() or (message.chat.title or "").strip()
        if not category:
            await message.reply("Укажите категорию: /register_chat <категория>")
            return
        await svc.register_chat(session, message.chat.id, message.chat.title or "", category)
        await session.commit()
    await message.reply(f"Чат зарегистрирован. Категория: «{category}».\n"
                        f"Упомяните меня в сообщении, чтобы создать тикет.")


@router.message(Command("unregister_chat"), GROUPS)
async def cmd_unregister_chat(message: Message) -> None:
    async with get_sessionmaker()() as session:
        if not await _require_admin(message, session):
            return
        chat = await svc.get_chat(session, message.chat.id)
        if chat is not None:
            await session.delete(chat)
            await session.commit()
    await message.reply("Чат отключён от CRM.")


@router.message(Command("set_support_chat"), GROUPS)
async def cmd_set_support_chat(message: Message) -> None:
    async with get_sessionmaker()() as session:
        if not await _require_admin(message, session):
            return
        await svc.set_support_chat(session, message.chat.id, message.chat.title or "")
        await session.commit()
    await message.reply("Этот чат назначен CRM-чатом поддержки. Сюда будут приходить карточки тикетов.")


@router.message(Command("add_operator"))
async def cmd_add_operator(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=1)
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is not None:
        tg_id, name = target.id, display_name(target)
    elif args and args[0].lstrip("-").isdigit():
        tg_id, name = int(args[0]), (args[1] if len(args) > 1 else "")
    else:
        await message.reply("Использование: /add_operator <tg_id> [имя] или реплаем на сообщение пользователя.")
        return
    async with get_sessionmaker()() as session:
        if not await _require_admin(message, session):
            return
        user = await svc.get_or_create_user(session, tg_id, name)
        if user.role != Role.ADMIN:
            user.role = Role.OPERATOR
        await session.commit()
    await message.reply(f"Оператор добавлен: {escape(name or str(tg_id))}", parse_mode="HTML")


@router.message(Command("remove_operator"))
async def cmd_remove_operator(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.reply("Использование: /remove_operator <tg_id>")
        return
    async with get_sessionmaker()() as session:
        if not await _require_admin(message, session):
            return
        user = await svc.get_user_by_tg(session, int(arg))
        if user is None or user.role != Role.OPERATOR:
            await message.reply("Такого оператора нет (админов удалить нельзя).")
            return
        user.role = Role.CLIENT
        await session.commit()
    await message.reply("Оператор удалён.")


# ---------- Операторы в CRM-чате ----------

async def _ticket_from_reply(session, message: Message):
    if not message.reply_to_message:
        return None
    return await svc.find_ticket_by_message(session, message.chat.id, message.reply_to_message.message_id)


async def _save_comment(message: Message, session, ticket, operator, text: str) -> None:
    msg = await svc.add_operator_message(session, ticket, operator=operator, text=text, kind=Kind.INTERNAL)
    await session.flush()
    sent = await message.reply(
        f"💬 Внутренний комментарий к тикету #{ticket.number} сохранён.",
        reply_markup=publish_keyboard(msg.id),
    )
    await svc.add_link(session, message.chat.id, message.message_id, ticket.id, msg.id)
    await svc.add_link(session, message.chat.id, sent.message_id, ticket.id, msg.id)


@router.message(Command("t"), GROUPS)
async def cmd_template(message: Message, command: CommandObject) -> None:
    async with get_sessionmaker()() as session:
        operator = await svc.get_staff(session, message.from_user.id)
        if operator is None:
            return
        name = (command.args or "").strip()
        if not name:
            names = list(await session.scalars(select(ReplyTemplate.name).order_by(ReplyTemplate.name)))
            await message.reply("Шаблоны: " + (", ".join(names) if names else "пока нет") +
                                "\nИспользование: реплай на карточку + /t <имя>")
            return
        tpl = await session.scalar(select(ReplyTemplate).where(ReplyTemplate.name == name))
        if tpl is None:
            await message.reply(f"Шаблон «{name}» не найден.")
            return
        ticket = await _ticket_from_reply(session, message)
        if ticket is None:
            await message.reply("Отправьте /t <имя> реплаем на карточку тикета.")
            return
        await _save_comment(message, session, ticket, operator, render(tpl.text, ticket_context(ticket)))
        await session.commit()


@router.message(Command("priority"), GROUPS)
async def cmd_priority(message: Message, command: CommandObject, bot: Bot) -> None:
    async with get_sessionmaker()() as session:
        if await svc.get_staff(session, message.from_user.id) is None:
            return
        priority = svc.parse_priority(command.args or "")
        ticket = await _ticket_from_reply(session, message)
        if priority is None or ticket is None:
            await message.reply("Использование: реплай на карточку + /priority "
                                + "|".join(Priority.LABELS.values()))
            return
        await svc.set_priority(session, ticket, priority)
        await refresh_card(bot, session, ticket)
        await session.commit()
    await message.reply(f"Приоритет тикета #{ticket.number}: {Priority.LABELS[priority]}")


@router.callback_query(F.data.startswith("st:"))
async def cb_status(cq: CallbackQuery, bot: Bot) -> None:
    _, ticket_id, status = cq.data.split(":", 2)
    async with get_sessionmaker()() as session:
        operator = await svc.get_staff(session, cq.from_user.id)
        if operator is None:
            await cq.answer("Нет доступа", show_alert=True)
            return
        if operator.name != display_name(cq.from_user):
            operator.name = display_name(cq.from_user)
        ticket = await svc.get_ticket(session, int(ticket_id))
        if ticket is None:
            await cq.answer("Тикет не найден", show_alert=True)
            return
        try:
            changed = await svc.set_status(session, ticket, status, operator)
        except svc.TransitionError as e:
            await cq.answer(str(e), show_alert=True)
            return
        if changed:
            await refresh_card(bot, session, ticket)
        await session.commit()
    await cq.answer(f"Статус: {Status.LABELS[status]}" if changed else "Без изменений")


@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(cq: CallbackQuery, bot: Bot) -> None:
    msg_id = int(cq.data.split(":", 1)[1])
    async with get_sessionmaker()() as session:
        if await svc.get_staff(session, cq.from_user.id) is None:
            await cq.answer("Нет доступа", show_alert=True)
            return
        msg = await session.get(DbMessage, msg_id)
        if msg is None:
            await cq.answer("Комментарий не найден", show_alert=True)
            return
        if msg.kind == Kind.PUBLIC:
            await cq.answer("Уже отправлено клиенту")
            return
        ticket = await svc.get_ticket(session, msg.ticket_id)
        await publish_to_client(bot, session, ticket, msg)
        await session.commit()
    await cq.answer("Отправлено клиенту")
    if cq.message:
        await cq.message.edit_text(f"✅ Ответ по тикету #{ticket.number} отправлен клиенту.")


# ---------- Перевод группы в супергруппу ----------

@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message) -> None:
    from app.chat_migration import migrate_chat_id
    await migrate_chat_id(message.chat.id, message.migrate_to_chat_id)


# ---------- Все остальные сообщения в группах ----------

@router.message(GROUPS)
async def on_group_message(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return
    # Анонимные админы и сообщения «от имени группы/канала» приходят от служебного бота
    # (GroupAnonymousBot) с заполненным sender_chat — их обрабатываем как обычные.
    if message.from_user.is_bot and message.sender_chat is None:
        return
    async with get_sessionmaker()() as session:
        chat = await svc.get_chat(session, message.chat.id)
        if chat is not None and chat.is_support_chat:
            await _on_support_message(message, session)
        else:
            await _on_work_message(message, bot, session, chat)
        await session.commit()


async def _on_support_message(message: Message, session) -> None:
    """Реплай оператора на карточку/сообщение тикета в CRM-чате = внутренний комментарий."""
    text = message_text(message)
    if text.startswith("/"):
        return
    ticket = await _ticket_from_reply(session, message)
    if ticket is None:
        return
    operator = await svc.get_staff(session, message.from_user.id)
    if operator is None:
        return
    if not text:
        return  # вложения без текста в комментариях не поддерживаем
    await _save_comment(message, session, ticket, operator, text)


async def _on_work_message(message: Message, bot: Bot, session, chat) -> None:
    me = await bot.me()
    is_mention = mentions_bot(message, me.username, me.id)

    if chat is None and is_mention:
        # Любой новый чат с ботом регистрируется автоматически; категория = название чата
        title = message.chat.title or str(message.chat.id)
        chat = await svc.register_chat(session, message.chat.id, title, title)
    if chat is None or chat.category_id is None:
        if is_mention:
            await message.reply("Этот чат отключён от CRM. Админ: /register_chat <категория>")
        return

    if message.sender_chat is not None:
        author = await svc.get_or_create_user(session, message.sender_chat.id,
                                              message.sender_chat.title or "Анонимно")
    else:
        author = await svc.get_or_create_user(session, message.from_user.id, display_name(message.from_user))
    text = strip_mention(message_text(message), me.username)
    attachments = extract_attachments(message)

    # 1) Реплай на сообщение, связанное с тикетом → в этот тикет
    ticket = await _ticket_from_reply(session, message)
    if ticket is not None:
        msg, reopened = await svc.add_client_message(
            session, ticket, author=author, text=text, tg_chat_id=message.chat.id,
            tg_message_id=message.message_id, attachments=attachments,
        )
        if message.media_group_id:
            _remember_group(message.media_group_id, ticket.id, msg.id)
        await notify_new_message(bot, session, ticket, msg, reopened=reopened,
                                 copy_from=(message.chat.id, message.message_id))
        return

    # 2) Продолжение альбома, начатого в тикете
    if message.media_group_id and message.media_group_id in _media_groups:
        ticket_id, msg_id = _media_groups[message.media_group_id]
        ticket = await svc.get_ticket(session, ticket_id)
        db_msg = await session.get(DbMessage, msg_id)
        if ticket and db_msg:
            for a in attachments:
                db_msg.attachments.append(Attachment(tg_file_id=a.tg_file_id, file_name=a.file_name,
                                                         mime=a.mime, file_type=a.file_type))
            await svc.add_link(session, message.chat.id, message.message_id, ticket.id, db_msg.id)
            await copy_to_support(bot, session, ticket, message.chat.id, message.message_id)
        return

    # 3) Упоминание бота вне ветки тикета → новый тикет
    if not is_mention:
        return
    ticket, msg = await svc.create_ticket(session, chat=chat, author=author, text=text,
                                          tg_message_id=message.message_id, attachments=attachments)
    if message.media_group_id:
        _remember_group(message.media_group_id, ticket.id, msg.id)
    reply = await message.reply(f"Создан тикет #{ticket.number}")
    await svc.add_link(session, message.chat.id, reply.message_id, ticket.id)
    await post_card(bot, session, ticket, text)
    if attachments:
        await copy_to_support(bot, session, ticket, message.chat.id, message.message_id)
