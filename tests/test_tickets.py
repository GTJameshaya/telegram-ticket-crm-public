import pytest

from app.models import Kind, Priority, Role, Status
from app.services import tickets as svc


async def _setup(session):
    chat = await svc.register_chat(session, -100, "Разработка", "Разработка")
    author = await svc.get_or_create_user(session, 42, "Иван")
    return chat, author


async def test_create_ticket_assigns_number_category_and_link(session):
    chat, author = await _setup(session)
    t1, m1 = await svc.create_ticket(session, chat=chat, author=author, text="Не работает", tg_message_id=10,
                                     attachments=[svc.AttachmentData("file1", "photo")])
    t2, _ = await svc.create_ticket(session, chat=chat, author=author, text="Ещё", tg_message_id=11)
    await session.commit()

    assert (t1.number, t2.number) == (1, 2)
    assert t1.status == Status.NEW and t1.priority == Priority.NORMAL
    assert t1.category.name == "Разработка"
    assert t1.source_chat_id == -100 and t1.source_message_id == 10
    assert (await svc.find_ticket_by_message(session, -100, 10)).id == t1.id
    loaded = await svc.get_ticket(session, t1.id)
    assert loaded.messages[0].kind == Kind.AUTHOR
    assert loaded.messages[0].attachments[0].tg_file_id == "file1"


async def test_reply_is_linked_to_ticket(session):
    chat, author = await _setup(session)
    ticket, _ = await svc.create_ticket(session, chat=chat, author=author, text="A", tg_message_id=10)
    msg, reopened = await svc.add_client_message(session, ticket, author=author, text="B",
                                                 tg_chat_id=-100, tg_message_id=12)
    await session.commit()
    assert not reopened
    # реплай на реплай тоже находит тикет
    assert (await svc.find_ticket_by_message(session, -100, 12)).id == ticket.id
    assert [m.text for m in (await svc.get_ticket(session, ticket.id)).messages] == ["A", "B"]
    assert await svc.find_ticket_by_message(session, -100, 999) is None


async def test_status_transitions_and_assignment(session):
    chat, author = await _setup(session)
    op = await svc.get_or_create_user(session, 7, "Оператор")
    op.role = Role.OPERATOR
    ticket, _ = await svc.create_ticket(session, chat=chat, author=author, text="A", tg_message_id=10)

    assert await svc.set_status(session, ticket, Status.IN_PROGRESS, op)
    assert ticket.assignee_id == op.id and ticket.assignee.name == "Оператор"
    assert await svc.set_status(session, ticket, Status.POSTPONED, op)
    assert await svc.set_status(session, ticket, Status.IN_PROGRESS, op)
    assert not await svc.set_status(session, ticket, Status.IN_PROGRESS, op)  # без изменений
    assert await svc.set_status(session, ticket, Status.CLOSED, op)
    with pytest.raises(svc.TransitionError):
        await svc.set_status(session, ticket, Status.NEW, op)
    with pytest.raises(svc.TransitionError):
        await svc.set_status(session, ticket, Status.POSTPONED, op)  # из закрытого — только в работу
    with pytest.raises(svc.TransitionError):
        await svc.set_status(session, ticket, "bogus", op)


async def test_new_message_reopens_closed_ticket(session):
    chat, author = await _setup(session)
    ticket, _ = await svc.create_ticket(session, chat=chat, author=author, text="A", tg_message_id=10)
    await svc.set_status(session, ticket, Status.CLOSED)
    msg, reopened = await svc.add_client_message(session, ticket, author=author, text="снова",
                                                 tg_chat_id=-100, tg_message_id=20)
    await session.commit()
    assert reopened
    t = await svc.get_ticket(session, ticket.id)
    assert t.status == Status.IN_PROGRESS
    assert t.number == 1 and len(t.messages) == 2  # тот же тикет


async def test_priority_parsing_and_setting(session):
    assert svc.parse_priority("Высокий") == Priority.HIGH
    assert svc.parse_priority("срочный") == Priority.URGENT
    assert svc.parse_priority("1") == Priority.LOW
    assert svc.parse_priority("normal") == Priority.NORMAL
    assert svc.parse_priority("??") is None
    chat, author = await _setup(session)
    ticket, _ = await svc.create_ticket(session, chat=chat, author=author, text="A", tg_message_id=10)
    await svc.set_priority(session, ticket, Priority.URGENT)
    assert ticket.priority_label == "Срочный"
    with pytest.raises(ValueError):
        await svc.set_priority(session, ticket, "x")


async def test_support_chat_is_unique_and_admin_seed(session):
    await svc.set_support_chat(session, -1, "CRM 1")
    await svc.set_support_chat(session, -2, "CRM 2")
    assert (await svc.get_support_chat(session)).tg_chat_id == -2
    await svc.ensure_admins(session, [1001, 1002])
    await svc.ensure_admins(session, [1001])  # идемпотентно
    assert (await svc.get_staff(session, 1001)).role == Role.ADMIN
    await svc.get_or_create_user(session, 5, "Клиент")
    assert await svc.get_staff(session, 5) is None
