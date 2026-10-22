"""Основная логика тикетов. Функции работают с сессией и делают flush; commit — на вызывающей стороне."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Attachment,
    Category,
    Chat,
    Kind,
    Message,
    MessageLink,
    Priority,
    Role,
    Status,
    Ticket,
    User,
    utcnow,
)


@dataclass
class AttachmentData:
    tg_file_id: str
    file_type: str = "document"
    file_name: str = ""
    mime: str = ""


class TransitionError(ValueError):
    pass


# Разрешённые переходы статусов (см. SPEC, раздел 5).
# «Новый» — только начальный статус; из «Закрыт» можно переоткрыть в «В работе».
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    Status.NEW: {Status.IN_PROGRESS, Status.POSTPONED, Status.CLOSED},
    Status.IN_PROGRESS: {Status.POSTPONED, Status.CLOSED},
    Status.POSTPONED: {Status.IN_PROGRESS, Status.CLOSED},
    Status.CLOSED: {Status.IN_PROGRESS},
}


# ---------- Пользователи ----------

async def get_user_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def get_or_create_user(session: AsyncSession, tg_id: int, name: str = "") -> User:
    user = await get_user_by_tg(session, tg_id)
    if user is None:
        user = User(tg_id=tg_id, name=name, role=Role.CLIENT)
        session.add(user)
        await session.flush()
    elif name and user.name != name:
        user.name = name
    return user


async def get_staff(session: AsyncSession, tg_id: int) -> User | None:
    """Оператор или админ по Telegram ID, иначе None."""
    user = await get_user_by_tg(session, tg_id)
    return user if user is not None and user.is_staff else None


async def ensure_admins(session: AsyncSession, tg_ids: list[int]) -> None:
    """Создаёт/повышает до админа пользователей из ADMIN_TG_IDS. Идемпотентно."""
    for tg_id in tg_ids:
        user = await get_user_by_tg(session, tg_id)
        if user is None:
            session.add(User(tg_id=tg_id, name="", role=Role.ADMIN))
        else:
            user.role = Role.ADMIN
    try:
        await session.commit()
    except IntegrityError:  # параллельный старт bot/web — не страшно
        await session.rollback()


# ---------- Чаты и категории ----------

async def get_or_create_category(session: AsyncSession, name: str) -> Category:
    name = name.strip()
    cat = await session.scalar(select(Category).where(Category.name == name))
    if cat is None:
        cat = Category(name=name)
        session.add(cat)
        await session.flush()
    return cat


async def get_chat(session: AsyncSession, tg_chat_id: int) -> Chat | None:
    return await session.scalar(select(Chat).where(Chat.tg_chat_id == tg_chat_id))


async def get_support_chat(session: AsyncSession) -> Chat | None:
    return await session.scalar(select(Chat).where(Chat.is_support_chat.is_(True)).limit(1))


async def register_chat(session: AsyncSession, tg_chat_id: int, title: str, category_name: str) -> Chat:
    """Регистрирует рабочий чат и привязывает к категории (создаёт категорию при необходимости)."""
    cat = await get_or_create_category(session, category_name)
    chat = await get_chat(session, tg_chat_id)
    if chat is None:
        chat = Chat(tg_chat_id=tg_chat_id, title=title, is_support_chat=False)
        session.add(chat)
    chat.title = title or chat.title
    chat.category_id = cat.id
    chat.category = cat
    chat.is_support_chat = False
    await session.flush()
    return chat


async def set_support_chat(session: AsyncSession, tg_chat_id: int, title: str) -> Chat:
    """Назначает CRM-чат поддержки (он может быть только один)."""
    for other in await session.scalars(select(Chat).where(Chat.is_support_chat.is_(True))):
        other.is_support_chat = False
    chat = await get_chat(session, tg_chat_id)
    if chat is None:
        chat = Chat(tg_chat_id=tg_chat_id, title=title)
        session.add(chat)
    chat.title = title or chat.title
    chat.is_support_chat = True
    chat.category_id = None
    await session.flush()
    return chat


# ---------- Связи сообщений ----------

async def add_link(
    session: AsyncSession, chat_id: int, message_id: int, ticket_id: int, db_message_id: int | None = None
) -> None:
    exists = await find_link(session, chat_id, message_id)
    if exists is None:
        session.add(MessageLink(chat_id=chat_id, message_id=message_id, ticket_id=ticket_id,
                                db_message_id=db_message_id))
        await session.flush()


async def find_link(session: AsyncSession, chat_id: int, message_id: int) -> MessageLink | None:
    return await session.scalar(
        select(MessageLink).where(MessageLink.chat_id == chat_id, MessageLink.message_id == message_id)
    )


async def find_ticket_by_message(session: AsyncSession, chat_id: int, message_id: int) -> Ticket | None:
    link = await find_link(session, chat_id, message_id)
    return await get_ticket(session, link.ticket_id) if link else None


# ---------- Тикеты ----------

async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(Ticket.id == ticket_id).options(selectinload(Ticket.messages))
        .execution_options(populate_existing=True)
    )


async def get_ticket_by_number(session: AsyncSession, number: int) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(Ticket.number == number).options(selectinload(Ticket.messages))
        .execution_options(populate_existing=True)
    )


async def next_ticket_number(session: AsyncSession) -> int:
    current = await session.scalar(select(func.max(Ticket.number)))
    return (current or 0) + 1


def _attach(message: Message, attachments: list[AttachmentData] | None) -> None:
    # Коллекцию задаём всегда, чтобы потом не было ленивой загрузки в async-контексте
    message.attachments = [
        Attachment(tg_file_id=a.tg_file_id, file_name=a.file_name, mime=a.mime, file_type=a.file_type)
        for a in attachments or []
    ]


async def create_ticket(
    session: AsyncSession,
    *,
    chat: Chat | None,
    author: User,
    text: str,
    tg_message_id: int | None,
    attachments: list[AttachmentData] | None = None,
    category: Category | None = None,
    meta: str = "",
) -> tuple[Ticket, Message]:
    """Новый тикет из сообщения в рабочем чате (категория = категория чата).

    Без чата (внешний источник через API) категорию передают явно, а исходного сообщения может не быть.
    """
    if chat is not None and category is None:
        category = chat.category
    ticket = Ticket(
        number=await next_ticket_number(session),
        status=Status.NEW,
        priority=Priority.NORMAL,
        category_id=category.id if category else None,
        author_id=author.id,
        source_chat_id=chat.tg_chat_id if chat else None,
        source_message_id=tg_message_id,
    )
    ticket.category = category
    ticket.author = author
    session.add(ticket)
    await session.flush()
    msg = Message(ticket_id=ticket.id, kind=Kind.AUTHOR, author=author, text=text, meta=meta or "",
                  tg_chat_id=chat.tg_chat_id if chat else None, tg_message_id=tg_message_id)
    _attach(msg, attachments)
    session.add(msg)
    await session.flush()
    if chat is not None and tg_message_id is not None:
        await add_link(session, chat.tg_chat_id, tg_message_id, ticket.id, msg.id)
    return ticket, msg


async def add_client_message(
    session: AsyncSession,
    ticket: Ticket,
    *,
    author: User | None,
    text: str,
    kind: str = Kind.AUTHOR,
    tg_chat_id: int | None = None,
    tg_message_id: int | None = None,
    attachments: list[AttachmentData] | None = None,
    meta: str = "",
) -> tuple[Message, bool]:
    """Сообщение со стороны клиента (реплай в рабочем чате или входящее письмо).

    Если тикет закрыт — переоткрывает его в «В работе». Возвращает (сообщение, переоткрыт_ли).
    """
    msg = Message(ticket_id=ticket.id, kind=kind, author=author, text=text, meta=meta or "",
                  tg_chat_id=tg_chat_id, tg_message_id=tg_message_id)
    _attach(msg, attachments)
    session.add(msg)
    reopened = False
    if ticket.status == Status.CLOSED:
        ticket.status = Status.IN_PROGRESS
        reopened = True
    ticket.updated_at = utcnow()
    await session.flush()
    if tg_chat_id is not None and tg_message_id is not None:
        await add_link(session, tg_chat_id, tg_message_id, ticket.id, msg.id)
    return msg, reopened


async def add_operator_message(
    session: AsyncSession, ticket: Ticket, *, operator: User | None, text: str, kind: str = Kind.INTERNAL
) -> Message:
    msg = Message(ticket_id=ticket.id, kind=kind, author=operator, text=text, attachments=[])
    session.add(msg)
    await session.flush()
    return msg


async def mark_published(session: AsyncSession, msg: Message, tg_chat_id: int, tg_message_id: int) -> None:
    """Комментарий опубликован клиенту: меняем вид и запоминаем, куда ушло сообщение."""
    msg.kind = Kind.PUBLIC
    msg.tg_chat_id = tg_chat_id
    msg.tg_message_id = tg_message_id
    await add_link(session, tg_chat_id, tg_message_id, msg.ticket_id, msg.id)
    await session.flush()


async def set_status(session: AsyncSession, ticket: Ticket, new_status: str, operator: User | None = None) -> bool:
    """Меняет статус. Возвращает False, если статус уже такой. «Взять в работу» назначает оператора."""
    if new_status not in Status.LABELS:
        raise TransitionError(f"Неизвестный статус: {new_status}")
    if ticket.status == new_status:
        if new_status == Status.IN_PROGRESS and operator and ticket.assignee_id != operator.id:
            ticket.assignee = operator
            await session.flush()
            return True
        return False
    if new_status not in ALLOWED_TRANSITIONS[ticket.status]:
        raise TransitionError(
            f"Нельзя перевести тикет из «{Status.LABELS[ticket.status]}» в «{Status.LABELS[new_status]}»"
        )
    ticket.status = new_status
    if new_status == Status.IN_PROGRESS and operator is not None:
        ticket.assignee = operator
    ticket.updated_at = utcnow()
    await session.flush()
    return True


async def set_priority(session: AsyncSession, ticket: Ticket, priority: str) -> None:
    if priority not in Priority.LABELS:
        raise ValueError(f"Неизвестный приоритет: {priority}")
    ticket.priority = priority
    await session.flush()


def parse_priority(value: str) -> str | None:
    """Принимает код (high), русское название (Высокий) или цифру 1–4."""
    v = value.strip().lower()
    codes = list(Priority.LABELS)
    if v in Priority.LABELS:
        return v
    if v.isdigit() and 1 <= int(v) <= len(codes):
        return codes[int(v) - 1]
    for code, label in Priority.LABELS.items():
        if label.lower() == v:
            return code
    return None


async def list_tickets(
    session: AsyncSession, *, status: str | None = None, category_id: int | None = None,
    priority: str | None = None, limit: int = 200,
) -> list[Ticket]:
    q = select(Ticket).order_by(Ticket.number.desc()).limit(limit)
    if status:
        q = q.where(Ticket.status == status)
    if category_id:
        q = q.where(Ticket.category_id == category_id)
    if priority:
        q = q.where(Ticket.priority == priority)
    return list(await session.scalars(q))


def first_text(ticket: Ticket) -> str:
    for m in ticket.messages:
        if m.kind == Kind.AUTHOR:
            return m.text
    return ""
