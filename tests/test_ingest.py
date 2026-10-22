"""POST /api/ingest/ticket — приём тикетов от внешнего бота (Telegram не доставляет ботам сообщения ботов)."""
import json

import pytest
from aiogram.methods import SendDocument, SendMessage
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.bot import handlers
from app.config import get_settings
from app.models import Attachment, Chat, Kind, MessageLink, Status
from app.services import tickets as svc
from app.telegram_ops import publish_to_client
from app.web import ingest
from app.web.main import app
from tests.conftest import BOT_USERNAME
from tests.test_bot_flow import AUTHOR, tg_message

TOKEN = "s3cr3t-ingest-token"
WORK = -1001234567890
SUPPORT = -100999
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CAPTION = (f"@{BOT_USERNAME} Привет! Показания за текущий 10.2026 — Тестовая 1 #Октябрь_2026 #Тестовая")


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(sessionmaker, bot, token):
    async with sessionmaker() as s:
        await svc.set_support_chat(s, SUPPORT, "CRM")
        await s.commit()
    app.state.bot = bot
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def post(client, data, files=None, headers=AUTH):
    return await client.post("/api/ingest/ticket", data=data, files=files, headers=headers)


# ---------- авторизация ----------

async def test_disabled_without_token(sessionmaker, bot, monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    get_settings.cache_clear()
    app.state.bot = bot
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/ingest/ticket", data={"text": "x"}, headers=AUTH)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": f"Basic {TOKEN}"}])
async def test_bad_token(client, headers, sessionmaker):
    r = await post(client, {"text": "x"}, headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert "INGEST_TOKEN" in r.json()["detail"]
    async with sessionmaker() as s:
        assert await svc.get_ticket_by_number(s, 1) is None


async def test_validation(client):
    assert (await post(client, {})).status_code == 422
    assert (await post(client, {"text": "  "})).status_code == 422
    assert (await post(client, {"text": "x", "chat_id": "abc"})).status_code == 422
    assert (await post(client, {"text": "x", "meta": "{bad"})).status_code == 422
    assert (await post(client, {"text": "x", "reply_to_ticket": "99"})).status_code == 404
    assert (await post(client, {"text": "x", "chat_id": str(SUPPORT)})).status_code == 422


def test_strip_leading_mention():
    assert ingest.strip_leading_mention("@Bot_X Привет", "bot_x") == "Привет"
    assert ingest.strip_leading_mention("  @bot_x, текст @bot_x", "bot_x") == "текст @bot_x"
    assert ingest.strip_leading_mention("@bot_xyz текст", "bot_x") == "@bot_xyz текст"


# ---------- создание ----------

async def test_create_in_known_chat_with_file_and_linking(client, bot, sessionmaker):
    async with sessionmaker() as s:
        await svc.register_chat(s, WORK, "ПОКАЗАНИЯ СЧЕТЧИКОВ", "Показания счетчиков")
        await s.commit()
    meta = {"house": "Тестовая 1", "period": "10.2026", "kind": "readings", "user_id": 12345}
    r = await post(client, {"text": CAPTION, "chat_id": str(WORK), "source_message_id": "321",
                            "author": "readings_bot", "meta": json.dumps(meta, ensure_ascii=False)},
                   files=[("files", ("Тестовая_1.xlsx", b"PK\x03\x04xlsx", "application/vnd.ms-excel"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ticket_number": 1, "ticket_id": body["ticket_id"], "created": True}

    # карточка в CRM-чате (с meta), документ реплаем на карточку
    card = bot.sent(SUPPORT)[0]
    assert "Новое сообщение в тикете #1" in card.text and "Показания счетчиков" in card.text
    assert "house: Тестовая 1" in card.text and "user_id: 12345" in card.text
    assert f"@{BOT_USERNAME}" not in card.text
    (doc,) = bot.of_type(SendDocument, SUPPORT)
    assert doc.reply_parameters.message_id == card._fake_result.message_id
    # «Создан тикет #1» в рабочем чате реплаем на сообщение внешнего бота
    (created,) = bot.sent(WORK)
    assert created.text == "Создан тикет #1" and created.reply_parameters.message_id == 321

    async with sessionmaker() as s:
        t = await svc.get_ticket_by_number(s, 1)
        assert t.category.name == "Показания счетчиков" and t.author.name == "readings_bot"
        assert (t.source_chat_id, t.source_message_id, t.status) == (WORK, 321, Status.NEW)
        m = t.messages[0]
        assert m.kind == Kind.AUTHOR and m.text.startswith("Привет! Показания за текущий 10.2026")
        assert json.loads(m.meta) == meta
        att = (await s.scalars(select(Attachment))).one()
        assert att.tg_file_id == doc._fake_result.document.file_id and att.file_name == "Тестовая_1.xlsx"
        links = {(lk.chat_id, lk.message_id) for lk in await s.scalars(select(MessageLink))}
        assert {(WORK, 321), (WORK, created._fake_result.message_id),
                (SUPPORT, card._fake_result.message_id), (SUPPORT, doc._fake_result.message_id)} <= links

    # реплай человека на сообщение внешнего бота → в тот же тикет
    reply = tg_message(bot, WORK, 400, "Принято, спасибо", reply_to=321)
    await handlers.on_group_message(reply, bot)
    # и реплай на «Создан тикет #1»
    await handlers.on_group_message(tg_message(bot, WORK, 401, "Ещё", reply_to=created._fake_result.message_id), bot)
    async with sessionmaker() as s:
        t = await svc.get_ticket_by_number(s, 1)
        assert [m.text for m in t.messages][1:] == ["Принято, спасибо", "Ещё"]
        assert t.messages[1].author.tg_id == AUTHOR["id"]
        # публичный ответ оператора уходит реплаем на сообщение внешнего бота
        op_msg = await svc.add_operator_message(s, t, operator=None, text="Показания учтены")
        await publish_to_client(bot, s, t, op_msg)
        await s.commit()
    pub = bot.sent(WORK)[-1]
    assert pub.text == "Ответ по тикету #1:\nПоказания учтены" and pub.reply_parameters.message_id == 321


async def test_create_unknown_chat_autoregisters_and_replies_to_bot_message(client, bot, sessionmaker):
    bot.chat_titles[WORK] = "Показания Тестовая"
    r = await post(client, {"text": "Показания", "chat_id": str(WORK)})
    assert r.status_code == 200 and r.json()["created"]
    (created,) = bot.sent(WORK)
    assert created.reply_parameters is None
    async with sessionmaker() as s:
        chat = await svc.get_chat(s, WORK)
        assert chat.title == "Показания Тестовая" and chat.category.name == "Показания Тестовая"
        t = await svc.get_ticket_by_number(s, 1)
        assert t.author.name == "API"
        # без source_message_id ответы операторов идут реплаем на «Создан тикет #N»
        assert t.source_message_id == created._fake_result.message_id
        op_msg = await svc.add_operator_message(s, t, operator=None, text="ок")
        await publish_to_client(bot, s, t, op_msg)
    assert bot.sent(WORK)[-1].reply_parameters.message_id == created._fake_result.message_id


async def test_create_without_chat_uses_category(client, bot, sessionmaker):
    r = await post(client, {"text": "Заявка с сайта"})
    assert r.status_code == 200
    r2 = await post(client, {"text": "Ещё", "category": "Сайт", "author": "Форма"})
    assert r2.json()["ticket_number"] == 2
    async with sessionmaker() as s:
        t1, t2 = await svc.get_ticket_by_number(s, 1), await svc.get_ticket_by_number(s, 2)
        assert t1.category.name == "Внешний источник" and t1.source_chat_id is None
        assert t2.category.name == "Сайт" and t2.author.name == "Форма"
    assert len(bot.sent(SUPPORT)) == 2 and all(c.chat_id == SUPPORT for c in bot.of_type(SendMessage))


async def test_disabled_chat_rejected(client, sessionmaker):
    async with sessionmaker() as s:
        s.add(Chat(tg_chat_id=WORK, title="off", category_id=None))
        await s.commit()
    assert (await post(client, {"text": "x", "chat_id": str(WORK)})).status_code == 409


async def test_file_too_large(client, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 10)
    r = await post(client, {"text": "x"}, files=[("files", ("big.bin", b"x" * 11, "application/octet-stream"))])
    assert r.status_code == 413


# ---------- дополнение ----------

async def test_append_to_ticket_reopens_and_links(client, bot, sessionmaker):
    r = await post(client, {"text": "Первое", "chat_id": str(WORK), "source_message_id": "10"})
    async with sessionmaker() as s:
        t = await svc.get_ticket_by_number(s, 1)
        await svc.set_status(s, t, Status.CLOSED)
        await s.commit()
    r = await post(client, {"text": f"@{BOT_USERNAME} Уточнённые показания", "reply_to_ticket": "1",
                            "chat_id": str(WORK), "source_message_id": "11", "meta": '{"kind": "fix"}'},
                   files=[("files", ("a.xlsx", b"1", "x/y")), ("files", ("b.xlsx", b"2", "x/y"))])
    assert r.status_code == 200
    assert r.json() == {"ticket_number": 1, "ticket_id": r.json()["ticket_id"], "created": False}
    note = bot.sent(SUPPORT)[-1]
    assert "Новое сообщение в тикете #1" in note.text and "переоткрыт" in note.text
    assert "Уточнённые показания" in note.text and "kind: fix" in note.text
    assert len(bot.of_type(SendDocument, SUPPORT)) == 2
    assert len(bot.sent(WORK)) == 1  # «Создан тикет» только при создании
    async with sessionmaker() as s:
        t = await svc.get_ticket_by_number(s, 1)
        assert t.status == Status.IN_PROGRESS
        assert [m.text for m in t.messages] == ["Первое", "Уточнённые показания"]
        assert [a.file_name for a in t.messages[1].attachments] == ["a.xlsx", "b.xlsx"]
        assert (await svc.find_ticket_by_message(s, WORK, 11)).id == t.id
