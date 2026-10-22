"""Сквозные сценарии обработчиков бота с фейковым Telegram API."""
from aiogram.methods import CopyMessage, EditMessageText
from aiogram.types import CallbackQuery, Message

from app.bot import handlers
from app.models import Kind, ReplyTemplate, Role, Status
from app.services import tickets as svc
from tests.conftest import BOT_USERNAME

WORK = -100111
SUPPORT = -100999
AUTHOR = {"id": 42, "is_bot": False, "first_name": "Иван", "username": "ivan"}
OPERATOR = {"id": 7, "is_bot": False, "first_name": "Оля"}


def tg_message(bot, chat_id, message_id, text=None, user=AUTHOR, reply_to=None, mention=False, **extra):
    data = {
        "message_id": message_id, "date": 0,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Чат"},
        "from": user,
    }
    if text is not None:
        data["text"] = text
        if mention:
            data["entities"] = [{"type": "mention", "offset": text.index("@"),
                                 "length": len(BOT_USERNAME) + 1}]
    if reply_to is not None:
        data["reply_to_message"] = {"message_id": reply_to, "date": 0,
                                    "chat": {"id": chat_id, "type": "supergroup"}}
    data.update(extra)
    return Message.model_validate(data, context={"bot": bot})


def callback(bot, data, user=OPERATOR, chat_id=SUPPORT, message_id=1):
    return CallbackQuery.model_validate({
        "id": "cb1", "from": user, "chat_instance": "x", "data": data,
        "message": {"message_id": message_id, "date": 0, "chat": {"id": chat_id, "type": "supergroup"},
                    "text": "..."},
    }, context={"bot": bot})


async def setup_chats(session):
    await svc.register_chat(session, WORK, "Разработка", "Разработка")
    await svc.set_support_chat(session, SUPPORT, "CRM")
    op = await svc.get_or_create_user(session, OPERATOR["id"], "Оля")
    op.role = Role.OPERATOR
    await session.commit()


async def create_ticket_via_bot(bot, text=f"@{BOT_USERNAME} не грузится отчёт", message_id=10):
    await handlers.on_group_message(tg_message(bot, WORK, message_id, text, mention=True), bot)


async def test_mention_creates_ticket_and_card(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)

    reply = bot.sent(WORK)[0]
    assert reply.text == "Создан тикет #1"
    assert reply.reply_parameters.message_id == 10
    card = bot.sent(SUPPORT)[0]
    assert "Новое сообщение в тикете #1" in card.text
    assert "Категория: Разработка" in card.text and "Статус: Новый" in card.text
    assert "Приоритет: Обычный" in card.text and "Иван" in card.text
    assert "не грузится отчёт" in card.text and f"@{BOT_USERNAME}" not in card.text
    buttons = [b.text for b in card.reply_markup.inline_keyboard[0]]
    assert buttons == ["Взять в работу", "Отложить", "Закрыть"]

    t = await svc.get_ticket_by_number(session, 1)
    assert t.card_message_id is not None
    # ответ бота «Создан тикет» тоже привязан к тикету
    assert await svc.find_link(session, WORK, 5000) is not None


async def test_message_without_mention_is_ignored(session, bot):
    await setup_chats(session)
    await handlers.on_group_message(tg_message(bot, WORK, 10, "просто болтаем"), bot)
    assert bot.calls == []


async def test_new_chat_mention_auto_registers_and_creates_ticket(session, bot):
    await setup_chats(session)
    await handlers.on_group_message(tg_message(bot, -555, 10, f"@{BOT_USERNAME} привет", mention=True), bot)
    assert "Создан тикет #1" in bot.sent(-555)[0].text
    chat = await svc.get_chat(session, -555)
    assert chat is not None and chat.category_id is not None
    assert await svc.get_ticket_by_number(session, 1) is not None


async def test_attachment_is_copied_to_support(session, bot):
    await setup_chats(session)
    msg = tg_message(bot, WORK, 10, None, caption=f"@{BOT_USERNAME} скрин",
                     caption_entities=[{"type": "mention", "offset": 0, "length": len(BOT_USERNAME) + 1}],
                     photo=[{"file_id": "small", "file_unique_id": "s", "width": 1, "height": 1},
                            {"file_id": "big", "file_unique_id": "b", "width": 9, "height": 9}])
    await handlers.on_group_message(msg, bot)
    copies = [c for c in bot.calls if isinstance(c, CopyMessage)]
    assert len(copies) == 1 and copies[0].chat_id == SUPPORT and copies[0].from_chat_id == WORK
    t = await svc.get_ticket_by_number(session, 1)
    assert t.messages[0].attachments[0].tg_file_id == "big"


async def test_reply_goes_to_ticket_and_notifies(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)
    bot.calls.clear()
    # реплай на ответ бота «Создан тикет #1» (message_id 5000)
    await handlers.on_group_message(tg_message(bot, WORK, 11, "ещё деталь", reply_to=5000), bot)
    note = bot.sent(SUPPORT)[0]
    assert "Новое сообщение в тикете #1" in note.text and "ещё деталь" in note.text
    t = await svc.get_ticket_by_number(session, 1)
    assert [m.text for m in t.messages][-1] == "ещё деталь"
    assert await svc.get_ticket_by_number(session, 2) is None

    # упоминание вне ветки тикета — новый тикет
    await create_ticket_via_bot(bot, f"@{BOT_USERNAME} другая проблема", message_id=12)
    assert (await svc.get_ticket_by_number(session, 2)) is not None


async def test_buttons_change_status_and_edit_card(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)
    t = await svc.get_ticket_by_number(session, 1)
    await handlers.cb_status(callback(bot, f"st:{t.id}:{Status.IN_PROGRESS}"), bot)
    edits = [c for c in bot.calls if isinstance(c, EditMessageText)]
    assert edits and "Статус: В работе" in edits[-1].text and "Исполнитель: Оля" in edits[-1].text
    assert edits[-1].message_id == t.card_message_id
    t = await svc.get_ticket(session, t.id)
    assert t.status == Status.IN_PROGRESS and t.assignee.tg_id == OPERATOR["id"]


async def test_non_operator_cannot_press_buttons(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)
    t = await svc.get_ticket_by_number(session, 1)
    await handlers.cb_status(callback(bot, f"st:{t.id}:{Status.CLOSED}", user=AUTHOR), bot)
    assert (await svc.get_ticket(session, t.id)).status == Status.NEW


async def test_reply_in_closed_ticket_reopens(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)
    t = await svc.get_ticket_by_number(session, 1)
    await handlers.cb_status(callback(bot, f"st:{t.id}:{Status.CLOSED}"), bot)
    await handlers.on_group_message(tg_message(bot, WORK, 11, "опять сломалось", reply_to=10), bot)
    t = await svc.get_ticket(session, t.id)
    assert t.status == Status.IN_PROGRESS and t.number == 1
    assert any("переоткрыт" in m.text for m in bot.sent(SUPPORT))


async def test_operator_comment_and_publish(session, bot):
    await setup_chats(session)
    await create_ticket_via_bot(bot)
    t = await svc.get_ticket_by_number(session, 1)
    bot.calls.clear()

    await handlers.on_group_message(
        tg_message(bot, SUPPORT, 50, "Посмотрим сегодня", user=OPERATOR, reply_to=t.card_message_id), bot)
    confirm = bot.sent(SUPPORT)[0]
    assert "Внутренний комментарий" in confirm.text
    button = confirm.reply_markup.inline_keyboard[0][0]
    assert button.text == "Отправить клиенту"
    t = await svc.get_ticket(session, t.id)
    comment = t.messages[-1]
    assert comment.kind == Kind.INTERNAL and comment.text == "Посмотрим сегодня"
    assert bot.sent(WORK) == []  # клиенту пока ничего не ушло

    await handlers.cb_publish(callback(bot, button.callback_data), bot)
    public = bot.sent(WORK)[0]
    assert "Посмотрим сегодня" in public.text and public.reply_parameters.message_id == 10
    await session.refresh(comment)
    assert comment.kind == Kind.PUBLIC
    # реплай клиента на опубликованный ответ — снова в тот же тикет
    await handlers.on_group_message(tg_message(bot, WORK, 60, "спасибо", reply_to=public_id(bot)), bot)
    t = await svc.get_ticket(session, t.id)
    assert t.messages[-1].text == "спасибо"


def public_id(bot):
    """message_id, который FakeBot вернул на публикацию ответа в рабочий чат."""
    return bot.sent(WORK)[0]._fake_result.message_id


async def test_template_command(session, bot):
    await setup_chats(session)
    session.add(ReplyTemplate(name="принято", text="Тикет #{ticket_id} принят, {author}"))
    await session.commit()
    await create_ticket_via_bot(bot)
    t = await svc.get_ticket_by_number(session, 1)
    from aiogram.filters import CommandObject
    msg = tg_message(bot, SUPPORT, 70, "/t принято", user=OPERATOR, reply_to=t.card_message_id)
    await handlers.cmd_template(msg, CommandObject(prefix="/", command="t", args="принято"))
    t = await svc.get_ticket(session, t.id)
    assert t.messages[-1].kind == Kind.INTERNAL
    assert t.messages[-1].text == "Тикет #1 принят, Иван (@ivan)"


async def test_anonymous_group_admin_mention_creates_ticket(session, bot):
    await setup_chats(session)
    anon = {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupAnonymousBot"}
    msg = tg_message(bot, WORK, 10, f"@{BOT_USERNAME} показания", user=anon, mention=True,
                     sender_chat={"id": WORK, "type": "supergroup", "title": "ПОКАЗАНИЯ СЧЕТЧИКОВ"})
    await handlers.on_group_message(msg, bot)
    t = await svc.get_ticket_by_number(session, 1)
    assert t is not None
    assert "Новое сообщение в тикете #1" in bot.sent(SUPPORT)[0].text
