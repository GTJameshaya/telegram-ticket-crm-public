"""Gmail API: отправка, приём, настройки. Сервис Gmail подменяется фейком."""
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.config import Settings
from app.mail_worker import main as worker
from app.models import Email, Kind, Message, Status
from app.services import gmail
from app.services import tickets as svc
from app.services.mail import send_ticket_email
from tests.fake_gmail import FakeGmailService

ME = "support@example.com"


def _settings(**kw) -> Settings:
    base = dict(gmail_user=ME, gmail_client_id="cid.apps.googleusercontent.com",
                gmail_client_secret="secret", gmail_refresh_token="1//refresh")
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def fake(monkeypatch):
    service = FakeGmailService()
    monkeypatch.setattr(gmail, "get_service", lambda settings: service)
    return service


def _parse(raw: bytes):
    import email
    import email.policy
    return email.message_from_bytes(raw, policy=email.policy.default)


def _raw(subject, body="Ответ", *, frm="Пётр <petr@example.com>", msg_id="<in1@example.com>", in_reply_to=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = ME
    m["Subject"] = subject
    m["Message-ID"] = msg_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


async def _ticket(session):
    chat = await svc.register_chat(session, -100, "Эксплуатация", "Эксплуатация")
    await svc.set_support_chat(session, -999, "CRM")
    author = await svc.get_or_create_user(session, 42, "Иван")
    t, _ = await svc.create_ticket(session, chat=chat, author=author, text="Протечка", tg_message_id=1)
    await session.commit()
    return await svc.get_ticket(session, t.id)


# ---------- Настройки ----------

def test_mail_enabled_requires_all_vars():
    assert _settings().mail_enabled
    s = _settings(gmail_refresh_token="", gmail_client_secret="")
    assert not s.mail_enabled
    assert s.gmail_api_missing_vars == ["GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]
    assert s.send_transport is None and s.receive_transport is None
    assert "GMAIL_REFRESH_TOKEN" in gmail.not_configured_message(s)
    assert _settings().send_transport == "gmail_api" and _settings().receive_transport == "gmail_api"


def test_poll_seconds_new_and_legacy_name(monkeypatch):
    monkeypatch.delenv("MAIL_POLL_SECONDS", raising=False)
    monkeypatch.setenv("IMAP_POLL_SECONDS", "60")
    assert Settings(_env_file=None).mail_poll_seconds == 60
    monkeypatch.setenv("MAIL_POLL_SECONDS", "120")
    assert Settings(_env_file=None).mail_poll_seconds == 120
    monkeypatch.delenv("MAIL_POLL_SECONDS")
    monkeypatch.delenv("IMAP_POLL_SECONDS")
    assert Settings(_env_file=None).mail_poll_seconds == 90


def test_get_service_not_configured():
    with pytest.raises(gmail.GmailError, match="Gmail API не настроен"):
        gmail.get_service(_settings(gmail_user=""))


def test_get_service_builds_offline():
    # discovery-документ Gmail встроен в google-api-python-client — сеть не нужна
    service = gmail.get_service(_settings())
    assert hasattr(service.users(), "messages")


def test_friendly_errors():
    from google.auth.exceptions import RefreshError
    assert "gmail_auth.py" in str(gmail._friendly(RefreshError("invalid_grant")))
    assert str(gmail._friendly(OSError("boom"))) == "Ошибка Gmail: boom"


def test_b64url_roundtrip():
    data = "Привет, мир!".encode() * 7
    assert gmail.b64url_decode(gmail.b64url_encode(data).rstrip("=")) == data


# ---------- Отправка ----------

async def test_send_not_configured(session):
    t = await _ticket(session)
    with pytest.raises(gmail.MailError, match="Почта не настроена"):
        await send_ticket_email(session, _settings(gmail_refresh_token=""), t, to="a@b.c", subject="x", body="y")


async def test_send_via_api_keeps_message_id(session, fake):
    t = await _ticket(session)
    rec = await send_ticket_email(session, _settings(), t, to="petr@example.com", subject="Акт", body="Текст")
    assert len(fake.sent) == 1
    assert b"\r\n" in fake.sent[0]  # RFC 5322: CRLF
    sent = _parse(fake.sent[0])
    assert sent["Subject"] == "[#1] Акт" and sent["From"] == ME and sent["In-Reply-To"] is None
    assert sent.get_content().strip() == "Текст"
    assert rec.message_id_header and rec.message_id_header == sent["Message-ID"]
    assert rec.direction == "out" and rec.to_addr == "petr@example.com"
    msgs = list(await session.scalars(select(Message).where(Message.kind == Kind.EMAIL_OUT)))
    assert len(msgs) == 1 and "Кому: petr@example.com" in msgs[0].text


async def test_send_stores_rewritten_message_id(session, fake):
    t = await _ticket(session)
    fake.rewrite_message_id = "<rewritten@mail.example.com>"
    rec = await send_ticket_email(session, _settings(), t, to="petr@example.com", subject="Акт", body="Текст")
    assert rec.message_id_header == "<rewritten@mail.example.com>"


async def test_send_reply_sets_threading_headers(session, fake):
    t = await _ticket(session)
    session.add(Email(ticket_id=t.id, direction="out", message_id_header="<out1@example.com>",
                      to_addr="petr@example.com", from_addr=ME, subject="[#1] Акт"))
    session.add(Email(ticket_id=t.id, direction="in", message_id_header="<in1@example.com>",
                      from_addr="petr@example.com", subject="Re: [#1] Акт"))
    await session.flush()
    await send_ticket_email(session, _settings(), t, to="Petr@Example.com", subject="Re: Акт", body="Ещё")
    sent = _parse(fake.sent[-1])
    assert sent["In-Reply-To"] == "<in1@example.com>"
    assert sent["References"] == "<out1@example.com> <in1@example.com>"
    assert sent["Subject"] == "[#1] Re: Акт"


# ---------- Приём ----------

async def test_poll_attaches_reply_reopens_and_marks_read(sessionmaker, bot, fake):
    settings = _settings()
    async with sessionmaker() as s:
        t = await _ticket(s)
        s.add(Email(ticket_id=t.id, direction="out", message_id_header="<out1@example.com>",
                    to_addr="petr@example.com", subject="[#1] Протечка", body="..."))
        await svc.set_status(s, t, Status.CLOSED)
        await s.commit()

    g_reply = fake.deliver(_raw("Re: Протечка", "Приедем завтра\n\nOn Mon, X wrote:\n> старое",
                                in_reply_to="<out1@example.com>", msg_id="<r1@example.com>"))
    g_tag = fake.deliver(_raw("Re: [#1] Протечка", "Ещё вопрос", msg_id="<r2@example.com>"))
    g_own = fake.deliver(_raw("[#1] Протечка", "наше", frm=f"CRM <{ME}>", msg_id="<own@example.com>"))
    g_spam = fake.deliver(_raw("Реклама", "купи", msg_id="<spam@x>"))
    g_read = fake.deliver(_raw("Re: [#1]", "старое", msg_id="<old@x>"), labels=("INBOX",))

    assert await worker.poll_once(settings, bot) == 2
    assert fake.list_calls == ["is:unread in:inbox"]
    marked = [i for call in fake.modify_calls for i in call["ids"]]
    assert sorted(marked) == sorted([g_reply, g_tag, g_own, g_spam])
    assert all(c["removeLabelIds"] == ["UNREAD"] for c in fake.modify_calls)
    assert all("UNREAD" not in fake.store[g]["labels"] for g in marked)
    assert g_read not in marked

    async with sessionmaker() as s:
        t = await svc.get_ticket(s, 1)
        assert t.status == Status.IN_PROGRESS
        ins = list(await s.scalars(select(Email).where(Email.direction == "in").order_by(Email.id)))
        assert [e.message_id_header for e in ins] == ["<r1@example.com>", "<r2@example.com>"]
        assert ins[0].body == "Приедем завтра"
    texts = [m.text for m in bot.sent(-999)]
    assert any("Приедем завтра" in x for x in texts)

    # повторная доставка того же письма (дубль) не создаёт запись
    fake.deliver(_raw("Re: [#1] Протечка", "Ещё вопрос", msg_id="<r2@example.com>"))
    assert await worker.poll_once(settings, bot) == 0
    async with sessionmaker() as s:
        assert len(list(await s.scalars(select(Email).where(Email.direction == "in")))) == 2


async def test_poll_skips_sent_label_and_paginates(sessionmaker, bot, fake):
    settings = _settings()
    async with sessionmaker() as s:
        await _ticket(s)
    fake.page_size = 2
    ids = [fake.deliver(_raw(f"Re: [#1] n{i}", f"текст {i}", msg_id=f"<p{i}@x>")) for i in range(5)]
    sent_to_self = fake.deliver(_raw("Re: [#1] self", "s", msg_id="<s@x>"), labels=("INBOX", "UNREAD", "SENT"))
    assert await worker.poll_once(settings, bot) == 5
    marked = {i for call in fake.modify_calls for i in call["ids"]}
    assert marked == set(ids) | {sent_to_self}
    async with sessionmaker() as s:
        ins = list(await s.scalars(select(Email).where(Email.direction == "in").order_by(Email.id)))
        assert [e.message_id_header for e in ins] == [f"<p{i}@x>" for i in range(5)]  # хронологически


async def test_worker_idles_when_not_configured(monkeypatch, caplog):
    import asyncio
    import logging

    monkeypatch.setattr(worker, "get_settings", lambda: _settings(gmail_client_id=""))

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", stop)
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await worker.main()
    assert "mail-worker простаивает" in caplog.text and "GMAIL_CLIENT_ID" in caplog.text
