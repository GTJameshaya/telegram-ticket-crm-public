"""Mini App: авторизация по initData и основные действия."""
import pytest
from aiogram.methods import EditMessageText
from httpx import ASGITransport, AsyncClient

from app.models import Kind, ReplyTemplate, Role, Status
from app.services import tickets as svc
from app.web.main import app
from tests.test_auth import make_init_data

ADMIN = 1001


@pytest.fixture
async def client(sessionmaker, bot):
    async with sessionmaker() as s:
        await svc.ensure_admins(s, [ADMIN])
        chat = await svc.register_chat(s, -100, "Разработка", "Разработка")
        await svc.set_support_chat(s, -999, "CRM")
        author = await svc.get_or_create_user(s, 42, "Иван")
        ticket, _ = await svc.create_ticket(s, chat=chat, author=author, text="Не работает", tg_message_id=10)
        ticket.card_message_id = 77
        s.add(ReplyTemplate(name="ok", text="Тикет {ticket_id} принят"))
        await s.commit()
    app.state.bot = bot
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.bot = None


def auth(user_id=ADMIN):
    return {"X-Telegram-Init-Data": make_init_data(user_id)}


async def test_shell_page_is_public(client):
    r = await client.get("/")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text


async def test_requires_valid_init_data(client):
    assert (await client.get("/ui/tickets")).status_code == 401
    bad = {"X-Telegram-Init-Data": make_init_data(ADMIN).replace("hash=", "hash=00")}
    assert (await client.get("/ui/tickets", headers=bad)).status_code == 401


async def test_non_operator_forbidden(client):
    r = await client.get("/ui/tickets", headers=auth(42))
    assert r.status_code == 403 and "только для операторов" in r.text


async def test_list_and_filter(client):
    r = await client.get("/ui/tickets", headers=auth())
    assert r.status_code == 200 and "#1" in r.text and "Новый" in r.text
    r = await client.get("/ui/tickets", params={"status": Status.CLOSED}, headers=auth())
    assert "#1" not in r.text and "Тикетов нет" in r.text


async def test_ticket_page_with_templates(client):
    r = await client.get("/ui/tickets/1", headers=auth())
    assert r.status_code == 200
    assert "Не работает" in r.text and "Видно клиенту" in r.text and "Тикет 1 принят" in r.text


async def test_status_change_updates_card(client, bot, sessionmaker):
    r = await client.post("/ui/tickets/1/status", data={"status": Status.IN_PROGRESS}, headers=auth())
    assert r.status_code == 200 and "Статус изменён" in r.text
    assert any(isinstance(c, EditMessageText) and c.message_id == 77 for c in bot.calls)
    async with sessionmaker() as s:
        t = await svc.get_ticket(s, 1)
        assert t.status == Status.IN_PROGRESS and t.assignee.tg_id == ADMIN


async def test_reply_internal_and_public(client, bot, sessionmaker):
    r = await client.post("/ui/tickets/1/reply", data={"text": "внутреннее"}, headers=auth())
    assert "Комментарий сохранён" in r.text
    assert bot.sent(-100) == []
    r = await client.post("/ui/tickets/1/reply", data={"text": "Починили", "visible": "1"}, headers=auth())
    assert "Ответ отправлен клиенту" in r.text
    sent = bot.sent(-100)[0]
    assert "Починили" in sent.text and sent.reply_parameters.message_id == 10
    async with sessionmaker() as s:
        kinds = [m.kind for m in (await svc.get_ticket(s, 1)).messages]
    assert kinds == [Kind.AUTHOR, Kind.INTERNAL, Kind.PUBLIC]


async def test_admin_crud_templates_and_users(client, sessionmaker):
    r = await client.post("/ui/settings/email-templates",
                          data={"name": "акт", "subject": "Акт", "text": "Тикет {ticket_id}"}, headers=auth())
    assert r.status_code == 200 and "акт" in r.text
    r = await client.post("/ui/users", data={"tg_id": "555", "name": "Пётр", "role": Role.OPERATOR},
                          headers=auth())
    assert "Пётр" in r.text
    async with sessionmaker() as s:
        assert (await svc.get_staff(s, 555)).role == Role.OPERATOR
    # оператору справочники недоступны
    r = await client.get("/ui/settings/chats", headers=auth(555))
    assert r.status_code == 403


async def test_email_form_renders_template(client):
    await client.post("/ui/settings/email-templates",
                      data={"name": "акт", "subject": "Акт по {category}", "text": "Текст: {text}"}, headers=auth())
    r = await client.get("/ui/tickets/1/email-form", params={"template_id": "1"}, headers=auth())
    assert "Акт по Разработка" in r.text and "Текст: Не работает" in r.text


async def test_email_send_not_configured_shows_russian_error(client):
    r = await client.post("/ui/tickets/1/email", data={"to": "a@b.c", "subject": "т", "body": "т"}, headers=auth())
    assert r.status_code == 200
    assert "Письмо не отправлено" in r.text and "Почта не настроена" in r.text and "GMAIL_REFRESH_TOKEN" in r.text


async def test_email_send_via_gmail_api(client, monkeypatch):
    from app.config import Settings
    from app.services import gmail
    from app.web import main as web_main
    from tests.fake_gmail import FakeGmailService

    fake = FakeGmailService()
    settings = Settings(_env_file=None, gmail_user="support@example.com", gmail_client_id="cid",
                        gmail_client_secret="sec", gmail_refresh_token="tok")
    monkeypatch.setattr(web_main, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail, "get_service", lambda s: fake)
    r = await client.post("/ui/tickets/1/email", data={"to": "petr@example.com", "subject": "Акт", "body": "Текст"},
                          headers=auth())
    assert "Письмо отправлено" in r.text and len(fake.sent) == 1
