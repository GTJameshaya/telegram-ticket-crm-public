"""Модель данных (см. docs/SPEC.md, раздел 11)."""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Справочники значений (код в БД -> подпись по-русски) ---

class Status:
    NEW = "new"
    IN_PROGRESS = "in_progress"
    POSTPONED = "postponed"
    CLOSED = "closed"

    LABELS = {
        NEW: "Новый",
        IN_PROGRESS: "В работе",
        POSTPONED: "Отложен",
        CLOSED: "Закрыт",
    }


class Priority:
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    LABELS = {
        LOW: "Низкий",
        NORMAL: "Обычный",
        HIGH: "Высокий",
        URGENT: "Срочный",
    }


class Role:
    CLIENT = "client"      # автор сообщений из рабочих чатов
    OPERATOR = "operator"
    ADMIN = "admin"

    LABELS = {CLIENT: "Клиент", OPERATOR: "Оператор", ADMIN: "Админ"}
    STAFF = (OPERATOR, ADMIN)


class Kind:
    AUTHOR = "author"        # сообщение автора / участника рабочего чата
    INTERNAL = "internal"    # внутренний комментарий оператора
    PUBLIC = "public"        # ответ оператора, видимый клиенту
    EMAIL_IN = "email_in"
    EMAIL_OUT = "email_out"

    LABELS = {
        AUTHOR: "Сообщение",
        INTERNAL: "Внутренний комментарий",
        PUBLIC: "Ответ клиенту",
        EMAIL_IN: "Входящее письмо",
        EMAIL_OUT: "Исходящее письмо",
    }


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    is_support_chat: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped[Category | None] = relationship(lazy="joined")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default=Role.CLIENT)

    @property
    def is_staff(self) -> bool:
        return self.role in Role.STAFF

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(20), default=Status.NEW)
    priority: Mapped[str] = mapped_column(String(20), default=Priority.NORMAL)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)       # Telegram chat id
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)    # Telegram message id
    card_message_id: Mapped[int | None] = mapped_column(BigInteger)      # карточка в CRM-чате
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    category: Mapped[Category | None] = relationship(lazy="joined")
    author: Mapped[User | None] = relationship(foreign_keys=[author_id], lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id], lazy="joined")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="ticket", order_by="Message.id", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return Status.LABELS.get(self.status, self.status)

    @property
    def priority_label(self) -> str:
        return Priority.LABELS.get(self.priority, self.priority)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, default="")
    # Где лежит сообщение в Telegram (для автора — исходное, для public — опубликованное)
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Произвольные метаданные (JSON-строка) от внешнего источника — POST /api/ingest/ticket
    meta: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
    author: Mapped[User | None] = relationship(lazy="joined")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def kind_label(self) -> str:
        return Kind.LABELS.get(self.kind, self.kind)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    tg_file_id: Mapped[str] = mapped_column(String(255))
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime: Mapped[str] = mapped_column(String(100), default="")
    # photo | document | video | voice | audio | video_note | animation
    file_type: Mapped[str] = mapped_column(String(20), default="document")

    message: Mapped[Message] = relationship(back_populates="attachments")


class MessageLink(Base):
    """Сообщение Telegram (chat_id, message_id) -> тикет. Нужна для привязки реплаев."""

    __tablename__ = "message_links"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_message_links_chat_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    # Если ссылка указывает на конкретную запись ленты (например, комментарий оператора)
    db_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    text: Mapped[str] = mapped_column(Text, default="")


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    message_id_header: Mapped[str] = mapped_column(String(255), default="", index=True)
    to_addr: Mapped[str] = mapped_column(String(255), default="")
    from_addr: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
