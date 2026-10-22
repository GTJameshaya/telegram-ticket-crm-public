"""Транспорт SMTP + IMAP (пароль приложения Gmail / SMTP-relay). smtplib и imaplib подменены фейками."""
import email
import email.policy
import imaplib
import smtplib
import socket
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.config import Settings
from app.mail_worker import main as worker
from app.models import Email, Status
from app.services import smtp_imap
from app.services import tickets as svc
from app.services.mail import send_ticket_email
from app.services.mail_errors import MailError

ME = "support@example.com"


def _settings(**kw) -> Settings:
    base = dict(gmail_user=ME, gmail_app_password="abcd efgh ijkl mnop")
    base.update(kw)
    return Settings(_env_file=None, **base)


# ---------- фейки ----------

class FakeSMTP:
    instances: list["FakeSMTP"] = []
    connect_error: Exception | None = None
    login_error: Exception | None = None
    starttls_supported = True

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.connect_error is not None:
            raise FakeSMTP.connect_error
        self.host, self.port, self.timeout = host, port, timeout
        self.ssl = False
        self.tls = False
        self.logins: list[tuple] = []
        self.sent: list[tuple] = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def has_extn(self, name):
        return name == "starttls" and FakeSMTP.starttls_supported

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error
        self.logins.append((user, password))

    def send_message(self, msg, from_addr=None):
        self.sent.append((from_addr, msg))

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


class FakeSMTPSSL(FakeSMTP):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ssl = True


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.connect_error = None
    FakeSMTP.login_error = None
    FakeSMTP.starttls_supported = True
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


class FakeMailbox:
    def __init__(self):
        self.messages: dict[bytes, dict] = {}
        self.logins: list[tuple] = []
        self.login_error = False
        self._uid = 0

    def deliver(self, raw: bytes, seen=False) -> bytes:
        self._uid += 1
        uid = str(self._uid).encode()
        self.messages[uid] = {"raw": raw, "seen": seen}
        return uid


class FakeIMAP:
    box: FakeMailbox

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def login(self, user, password):
        if self.box.login_error:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.box.logins.append((user, password))
        return "OK", [b"ok"]

    def select(self, mailbox):
        assert mailbox == "INBOX"
        return "OK", [str(len(self.box.messages)).encode()]

    def uid(self, command, *args):
        if command == "search":
            assert args[-1] == "UNSEEN"
            uids = [u for u, m in self.box.messages.items() if not m["seen"]]
            return "OK", [b" ".join(uids)]
        if command == "fetch":
            uid, what = args
            assert what == "(BODY.PEEK[])"  # не помечает прочитанным
            return "OK", [(uid + b" (UID " + uid + b" BODY[] {n})", self.box.messages[uid]["raw"]), b")"]
        if command == "store":
            uid, op, flags = args
            assert op == "+FLAGS" and flags == "(\\Seen)"
            self.box.messages[uid]["seen"] = True
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]

    def shutdown(self):
        pass


@pytest.fixture
def mailbox(monkeypatch):
    box = FakeMailbox()
    FakeIMAP.box = box
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return box


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


# ---------- выбор транспорта ----------

def test_transport_selection():
    api = dict(gmail_client_id="cid", gmail_client_secret="sec", gmail_refresh_token="tok")
    s = _settings(**api)  # пароль приложения важнее Gmail API
    assert (s.send_transport, s.receive_transport) == ("smtp", "imap")
    s = _settings(gmail_app_password="", **api)
    assert (s.send_transport, s.receive_transport) == ("gmail_api", "gmail_api")
    s = _settings(gmail_app_password="")
    assert (s.send_transport, s.receive_transport) == (None, None) and not s.mail_enabled
    # relay для отправки + Gmail API для приёма
    s = _settings(gmail_app_password="", smtp_host="smtp-relay.example.com", smtp_port=2525,
                  smtp_user="u", smtp_password="p", **api)
    assert (s.send_transport, s.receive_transport) == ("smtp", "gmail_api")
    assert s.smtp_credentials == ("u", "p")


def test_smtp_defaults_and_empty_env(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "IMAP_HOST", "IMAP_PORT"):
        monkeypatch.setenv(k, "")
    s = Settings(_env_file=None)
    assert (s.smtp_host, s.smtp_port, s.imap_host, s.imap_port) == ("smtp.gmail.com", 587, "imap.gmail.com", 993)
    assert _settings().smtp_credentials == (ME, "abcd efgh ijkl mnop")
    # сторонний relay без логина — без авторизации (пароль Gmail туда не отправляем)
    assert _settings(smtp_host="relay.local", smtp_port=2525).smtp_credentials is None


# ---------- отправка ----------

async def test_smtp_send_starttls_587(session, fake_smtp):
    t = await _ticket(session)
    rec = await send_ticket_email(session, _settings(), t, to="petr@example.com", subject="Акт", body="Текст")
    (conn,) = fake_smtp.instances
    assert (conn.host, conn.port, conn.ssl, conn.tls) == ("smtp.gmail.com", 587, False, True)
    assert conn.logins == [(ME, "abcd efgh ijkl mnop")] and conn.closed
    from_addr, msg = conn.sent[0]
    assert from_addr == ME and msg["Subject"] == "[#1] Акт" and msg["To"] == "petr@example.com"
    assert rec.message_id_header == msg["Message-ID"] and rec.direction == "out"


async def test_smtp_send_ssl_465_and_reply_headers(session, fake_smtp):
    t = await _ticket(session)
    session.add(Email(ticket_id=t.id, direction="in", message_id_header="<in1@example.com>",
                      from_addr="petr@example.com", subject="Re: [#1] Акт"))
    await session.flush()
    await send_ticket_email(session, _settings(smtp_port=465), t, to="petr@example.com", subject="Re: Акт", body="x")
    conn = fake_smtp.instances[0]
    assert conn.ssl and not conn.tls and conn.port == 465
    msg = conn.sent[0][1]
    assert msg["In-Reply-To"] == "<in1@example.com>" and msg["References"] == "<in1@example.com>"


async def test_smtp_relay_2525(session, fake_smtp):
    t = await _ticket(session)
    s = _settings(gmail_app_password="", smtp_host="smtp-relay.example.com", smtp_port=2525,
                  smtp_user="relay-user", smtp_password="relay-pass")
    await send_ticket_email(session, s, t, to="a@b.c", subject="т", body="т")
    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port, conn.tls) == ("smtp-relay.example.com", 2525, True)
    assert conn.logins == [("relay-user", "relay-pass")] and conn.sent[0][0] == ME


def test_smtp_refuses_plaintext_auth(fake_smtp):
    fake_smtp.starttls_supported = False
    msg = EmailMessage()
    msg["Message-ID"] = "<x@y>"
    with pytest.raises(MailError, match="STARTTLS"):
        smtp_imap.smtp_send(_settings(smtp_host="relay.local", smtp_port=2525, smtp_password="p"), msg)
    assert fake_smtp.instances[0].closed


@pytest.mark.parametrize("err", [TimeoutError("timed out"), socket.timeout("timed out"),
                                 ConnectionRefusedError(111, "refused"), OSError(101, "Network is unreachable")])
async def test_smtp_blocked_fails_gracefully(session, fake_smtp, err, caplog):
    t = await _ticket(session)
    fake_smtp.connect_error = err
    with pytest.raises(smtp_imap.SmtpBlockedError) as exc:
        await send_ticket_email(session, _settings(), t, to="a@b.c", subject="т", body="т")
    assert "Отправка писем недоступна: SMTP-порты заблокированы хостингом" in str(exc.value)
    assert "smtp.gmail.com:587" in str(exc.value)
    assert "SMTP-порты заблокированы" in caplog.text  # залогировано
    assert list(await session.scalars(select(Email))) == []  # ничего не сохранили


def test_smtp_auth_error(fake_smtp):
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    msg = EmailMessage()
    msg["Message-ID"] = "<x@y>"
    with pytest.raises(MailError, match="пароль приложения"):
        smtp_imap.smtp_send(_settings(), msg)


# ---------- приём ----------

async def test_imap_poll_attaches_reopens_dedupes_and_marks_seen(sessionmaker, bot, mailbox):
    settings = _settings()
    async with sessionmaker() as s:
        t = await _ticket(s)
        s.add(Email(ticket_id=t.id, direction="out", message_id_header="<out1@example.com>",
                    to_addr="petr@example.com", subject="[#1] Протечка", body="..."))
        await svc.set_status(s, t, Status.CLOSED)
        await s.commit()

    u_reply = mailbox.deliver(_raw("Re: Протечка", "Приедем завтра\n\nOn Mon, X wrote:\n> старое",
                                   in_reply_to="<out1@example.com>", msg_id="<r1@example.com>"))
    u_tag = mailbox.deliver(_raw("Re: [#1] Протечка", "Ещё вопрос", msg_id="<r2@example.com>"))
    u_own = mailbox.deliver(_raw("[#1] Протечка", "наше", frm=f"CRM <{ME}>", msg_id="<own@example.com>"))
    u_spam = mailbox.deliver(_raw("Реклама", "купи", msg_id="<spam@x>"))
    u_old = mailbox.deliver(_raw("Re: [#1]", "старое", msg_id="<old@x>"), seen=True)

    assert await worker.poll_once(settings, bot) == 2
    assert all(mailbox.messages[u]["seen"] for u in (u_reply, u_tag, u_own, u_spam, u_old))
    assert mailbox.logins and all(x == (ME, "abcd efgh ijkl mnop") for x in mailbox.logins)

    async with sessionmaker() as s:
        assert (await svc.get_ticket(s, 1)).status == Status.IN_PROGRESS
        ins = list(await s.scalars(select(Email).where(Email.direction == "in").order_by(Email.id)))
        assert [e.message_id_header for e in ins] == ["<r1@example.com>", "<r2@example.com>"]
        assert ins[0].body == "Приедем завтра"
    assert any("Приедем завтра" in m.text for m in bot.sent(-999))

    mailbox.deliver(_raw("Re: [#1] Протечка", "Ещё вопрос", msg_id="<r2@example.com>"))  # дубль
    assert await worker.poll_once(settings, bot) == 0
    assert all(m["seen"] for m in mailbox.messages.values())


async def test_imap_works_while_smtp_blocked(sessionmaker, bot, mailbox, fake_smtp):
    fake_smtp.connect_error = TimeoutError("timed out")
    async with sessionmaker() as s:
        await _ticket(s)
    mailbox.deliver(_raw("Re: [#1] Протечка", "Ответ клиента", msg_id="<r9@x>"))
    assert await worker.poll_once(_settings(), bot) == 1


def test_imap_login_error(mailbox):
    mailbox.login_error = True
    with pytest.raises(MailError, match="IMAP: Gmail отклонил"):
        smtp_imap.fetch_unseen(_settings())


async def test_web_shows_blocked_smtp_error(sessionmaker, bot, fake_smtp, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.web import main as web_main
    from tests.test_web import ADMIN, auth

    async with sessionmaker() as s:
        await svc.ensure_admins(s, [ADMIN])
        await _ticket(s)
    fake_smtp.connect_error = TimeoutError("timed out")
    monkeypatch.setattr(web_main, "get_settings", lambda: _settings())
    web_main.app.state.bot = bot
    async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as c:
        r = await c.post("/ui/tickets/1/email", data={"to": "a@b.c", "subject": "т", "body": "т"}, headers=auth())
    assert r.status_code == 200
    assert "Письмо не отправлено: Отправка писем недоступна: SMTP-порты заблокированы хостингом" in r.text
