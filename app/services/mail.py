"""Почта: шаблоны писем, метка [#N] в теме, отправка (SMTP или Gmail API), разбор входящих писем."""
import asyncio
import email
import email.policy
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services import gmail, smtp_imap
from app.services.mail_errors import MailError, not_configured_message
from app.models import Email, Kind, Message, Ticket, User
from app.services import tickets as svc

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"\[#(\d+)\]")


def subject_with_tag(subject: str, number: int) -> str:
    """Добавляет метку [#N] в начало темы, если её ещё нет."""
    tag = f"[#{number}]"
    subject = (subject or "").strip()
    if tag in subject:
        return subject
    return f"{tag} {subject}".strip()


def parse_ticket_number(subject: str) -> int | None:
    m = TAG_RE.search(subject or "")
    return int(m.group(1)) if m else None


class _SafeDict(dict):
    def __missing__(self, key):  # неизвестные плейсхолдеры оставляем как есть
        return "{" + key + "}"


def render(template: str, context: dict) -> str:
    try:
        return (template or "").format_map(_SafeDict(context))
    except (ValueError, IndexError, AttributeError):
        # Например, одиночная «{» в тексте — тогда делаем простую замену
        out = template or ""
        for k, v in context.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def ticket_context(ticket: Ticket, comment: str = "") -> dict:
    """Плейсхолдеры, доступные в шаблонах писем и быстрых ответов."""
    return {
        "ticket_id": ticket.number,
        "number": ticket.number,
        "text": svc.first_text(ticket),
        "author": ticket.author.name if ticket.author else "",
        "category": ticket.category.name if ticket.category else "",
        "priority": ticket.priority_label,
        "status": ticket.status_label,
        "assignee": ticket.assignee.name if ticket.assignee else "",
        "comment": comment,
    }


# ---------- Отправка ----------

def _gmail_send(settings: Settings, msg: EmailMessage) -> str:
    """Отправляет письмо через Gmail API и возвращает итоговый Message-ID.

    Gmail обычно сохраняет наш Message-ID, но может его переписать — тогда берём заголовок
    у отправленного письма, чтобы ответы находились по In-Reply-To/References.
    """
    ours = str(msg["Message-ID"])
    service = gmail.get_service(settings)
    sent = gmail.send_raw(settings, msg.as_bytes(policy=email.policy.SMTP), service=service)
    gid = sent.get("id")
    if gid:
        try:
            actual = gmail.get_message_id_header(settings, gid, service=service)
        except gmail.GmailError:
            log.warning("Не удалось прочитать Message-ID отправленного письма %s, сохраняем свой", gid)
            actual = ""
        if actual and actual != ours:
            return actual
    return ours


async def _reply_headers(session: AsyncSession, ticket: Ticket, to: str) -> tuple[str, list[str]]:
    """Если по тикету уже была переписка с этим адресом — (In-Reply-To, References) для ответа в ветку."""
    rows = list(await session.scalars(
        select(Email).where(Email.ticket_id == ticket.id, Email.message_id_header != "")
        .order_by(Email.created_at, Email.id)))
    to_l = to.strip().lower()
    related = [r for r in rows if to_l in ((r.from_addr or "").lower(), (r.to_addr or "").lower())]
    if not related:
        return "", []
    refs: list[str] = []
    for r in related:
        if r.message_id_header not in refs:
            refs.append(r.message_id_header)
    return related[-1].message_id_header, refs[-20:]


async def send_ticket_email(
    session: AsyncSession, settings: Settings, ticket: Ticket, *, to: str, subject: str, body: str,
    operator: User | None = None,
) -> Email:
    """Отправляет письмо по тикету (тема с меткой [#N]) и сохраняет его в ленту."""
    transport = settings.send_transport
    if transport is None:
        raise MailError(not_configured_message(settings))
    subject = subject_with_tag(subject, ticket.number)
    domain = settings.gmail_user.split("@")[-1] or "localhost"
    msg = EmailMessage()
    msg["From"] = settings.gmail_user
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(idstring=f"ticket{ticket.number}", domain=domain)
    in_reply_to, refs = await _reply_headers(session, ticket, to)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    sender = smtp_imap.smtp_send if transport == "smtp" else _gmail_send
    message_id = await asyncio.to_thread(sender, settings, msg)

    rec = Email(ticket_id=ticket.id, direction="out", message_id_header=message_id,
                to_addr=to, from_addr=settings.gmail_user, subject=subject, body=body)
    session.add(rec)
    session.add(Message(ticket_id=ticket.id, kind=Kind.EMAIL_OUT, author_id=operator.id if operator else None,
                        text=f"Кому: {to}\nТема: {subject}\n\n{body}"))
    await session.flush()
    return rec


# ---------- Приём ----------

@dataclass
class IncomingEmail:
    message_id: str
    subject: str
    from_addr: str
    body: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)


_QUOTE_HEADER_RE = re.compile(
    r"^(On .+wrote:|.+ пишет:|.+ написал\(а\):|-----Original Message-----|-{2,} ?Исходное сообщение ?-{2,})\s*$",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Отрезает цитату предыдущего письма (строки с «>» и всё после «On ... wrote:»)."""
    lines = []
    for line in (text or "").splitlines():
        if _QUOTE_HEADER_RE.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_email(raw: bytes) -> IncomingEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    refs = str(msg.get("References", "") or "").split()
    return IncomingEmail(
        message_id=str(msg.get("Message-ID", "") or "").strip(),
        subject=str(msg.get("Subject", "") or ""),
        from_addr=parseaddr(str(msg.get("From", "") or ""))[1],
        body=strip_quoted(body),
        in_reply_to=str(msg.get("In-Reply-To", "") or "").strip(),
        references=refs,
    )


async def find_ticket_for_email(session: AsyncSession, inc: IncomingEmail) -> Ticket | None:
    """Сначала по заголовкам In-Reply-To/References, затем по метке [#N] в теме."""
    ids = [i for i in [inc.in_reply_to, *reversed(inc.references)] if i]
    if ids:
        rec = await session.scalar(select(Email).where(Email.message_id_header.in_(ids)).limit(1))
        if rec is not None:
            return await svc.get_ticket(session, rec.ticket_id)
    number = parse_ticket_number(inc.subject)
    if number is not None:
        return await svc.get_ticket_by_number(session, number)
    return None


async def process_incoming(session: AsyncSession, inc: IncomingEmail) -> tuple[Ticket, Message, bool] | None:
    """Прикрепляет письмо к тикету. None — если тикет не найден или письмо уже обработано."""
    if inc.message_id:
        dup = await session.scalar(select(Email).where(Email.message_id_header == inc.message_id))
        if dup is not None:
            return None
    ticket = await find_ticket_for_email(session, inc)
    if ticket is None:
        return None
    session.add(Email(ticket_id=ticket.id, direction="in", message_id_header=inc.message_id,
                      from_addr=inc.from_addr, subject=inc.subject, body=inc.body))
    msg, reopened = await svc.add_client_message(
        session, ticket, author=None, kind=Kind.EMAIL_IN,
        text=f"От: {inc.from_addr}\nТема: {inc.subject}\n\n{inc.body}",
    )
    return ticket, msg, reopened
