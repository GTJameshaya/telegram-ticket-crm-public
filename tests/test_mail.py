from email.message import EmailMessage

from app.models import Kind, Status
from app.services import tickets as svc
from app.services.mail import (
    IncomingEmail,
    parse_email,
    parse_ticket_number,
    process_incoming,
    render,
    strip_quoted,
    subject_with_tag,
    ticket_context,
)
from app.models import Email


def test_subject_tag():
    assert subject_with_tag("Заявка", 5) == "[#5] Заявка"
    assert subject_with_tag("Re: [#5] Заявка", 5) == "Re: [#5] Заявка"
    assert subject_with_tag("", 7) == "[#7]"


def test_parse_ticket_number():
    assert parse_ticket_number("Re: [#12] Счётчики") == 12
    assert parse_ticket_number("RE: FW: [#3]") == 3
    assert parse_ticket_number("Без метки #12") is None
    assert parse_ticket_number("") is None


def test_render_placeholders():
    out = render("Тикет {ticket_id} от {author}: {text} {unknown}", {"ticket_id": 3, "author": "Иван", "text": "т"})
    assert out == "Тикет 3 от Иван: т {unknown}"
    # одиночная фигурная скобка не ломает шаблон
    assert render("a { b {ticket_id}", {"ticket_id": 1}) == "a { b 1"


def test_strip_quoted():
    body = "Спасибо, сделаем.\n\nOn Mon, 1 Jan 2026 at 10:00, CRM <support@example.com> wrote:\n> старое"
    assert strip_quoted(body) == "Спасибо, сделаем."
    assert strip_quoted("Ок\n> цитата\nещё") == "Ок\nещё"


def _raw(subject, body="Ответ", in_reply_to=None, msg_id="<in1@example.com>"):
    m = EmailMessage()
    m["From"] = "Пётр <petr@example.com>"
    m["To"] = "support@example.com"
    m["Subject"] = subject
    m["Message-ID"] = msg_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


def test_parse_email():
    inc = parse_email(_raw("Re: [#4] Тема", "Готово\n> старое", in_reply_to="<out1@example.com>"))
    assert inc.from_addr == "petr@example.com"
    assert inc.subject == "Re: [#4] Тема"
    assert inc.body == "Готово"
    assert inc.in_reply_to == "<out1@example.com>"
    assert inc.message_id == "<in1@example.com>"


async def _ticket(session):
    chat = await svc.register_chat(session, -100, "Эксплуатация", "Эксплуатация")
    author = await svc.get_or_create_user(session, 42, "Иван")
    t, _ = await svc.create_ticket(session, chat=chat, author=author, text="Протечка", tg_message_id=1)
    return t


async def test_context(session):
    t = await _ticket(session)
    t = await svc.get_ticket(session, t.id)
    ctx = ticket_context(t)
    assert ctx["ticket_id"] == 1 and ctx["text"] == "Протечка" and ctx["author"] == "Иван"
    assert ctx["category"] == "Эксплуатация"


async def test_incoming_by_subject_tag_and_dedup(session):
    t = await _ticket(session)
    await svc.create_ticket(session, chat=await svc.get_chat(session, -100),
                            author=await svc.get_or_create_user(session, 42), text="другой", tg_message_id=2)
    inc = parse_email(_raw("Re: [#1] Протечка", "Приедем завтра"))
    ticket, msg, reopened = await process_incoming(session, inc)
    assert ticket.id == t.id and msg.kind == Kind.EMAIL_IN and "Приедем завтра" in msg.text
    assert not reopened
    assert await process_incoming(session, inc) is None  # повторно не добавляется


async def test_incoming_by_in_reply_to_and_reopen(session):
    t = await _ticket(session)
    session.add(Email(ticket_id=t.id, direction="out", message_id_header="<out1@example.com>",
                      to_addr="petr@example.com", subject="[#1] Протечка", body="..."))
    await svc.set_status(session, t, Status.CLOSED)
    # тема без метки — находим по In-Reply-To
    inc = parse_email(_raw("Re: Протечка", "Ещё вопрос", in_reply_to="<out1@example.com>", msg_id="<in2@x>"))
    ticket, msg, reopened = await process_incoming(session, inc)
    assert ticket.id == t.id and reopened
    assert (await svc.get_ticket(session, t.id)).status == Status.IN_PROGRESS


async def test_incoming_unknown_ticket(session):
    await _ticket(session)
    assert await process_incoming(session, IncomingEmail("<x@y>", "Привет", "a@b.c", "текст")) is None
    assert await process_incoming(session, IncomingEmail("<x2@y>", "[#99] нет такого", "a@b.c", "т")) is None
