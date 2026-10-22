"""Telegram Mini App (FastAPI + Jinja2 + HTMX).

Все страницы, кроме оболочки «/», отдаются частями (partials) и загружаются HTMX в #main.
Каждый запрос несёт заголовок X-Telegram-Init-Data, который проверяется по токену бота.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from aiogram import Bot
from app.chat_migration import install as install_migration
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InitDataError, verify_init_data
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import (
    Attachment,
    Category,
    Chat,
    EmailTemplate,
    Kind,
    Priority,
    ReplyTemplate,
    Role,
    Status,
    User,
)
from app.services import tickets as svc
from app.services.mail import render, send_ticket_email, ticket_context
from app.telegram_ops import meta_text, publish_to_client, refresh_card

log = logging.getLogger(__name__)
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(Status=Status, Priority=Priority, Kind=Kind, Role=Role)


def _fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite отдаёт naive-время в UTC
        value = value.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(get_settings().timezone)
    except Exception:
        tz = timezone.utc
    return value.astimezone(tz).strftime(fmt)


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["meta"] = meta_text


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.bot = install_migration(Bot(settings.bot_token)) if settings.bot_token else None
    try:
        async with get_sessionmaker()() as session:
            await svc.ensure_admins(session, settings.admin_tg_ids)
    except Exception:
        log.exception("Не удалось создать админов из ADMIN_TG_IDS")
    yield
    if app.state.bot:
        await app.state.bot.session.close()


app = FastAPI(title="Telegram Ticket CRM", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

from app.web.ingest import router as ingest_router  # noqa: E402

app.include_router(ingest_router)


# ---------- Зависимости ----------

async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, session: Session) -> User:
    settings = get_settings()
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query_params.get("init_data", "")
    try:
        tg = verify_init_data(init_data, settings.bot_token, settings.webapp_auth_max_age)
    except InitDataError as e:
        raise HTTPException(401, str(e))
    user = await svc.get_staff(session, int(tg["id"]))
    if user is None:
        raise HTTPException(403, "Доступ только для операторов CRM. Попросите админа добавить ваш Telegram ID.")
    name = " ".join(x for x in (tg.get("first_name"), tg.get("last_name")) if x)
    if tg.get("username"):
        name = f"{name} (@{tg['username']})"
    if name and user.name != name:
        user.name = name
        await session.commit()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def admin_user(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Раздел доступен только админам.")
    return user


AdminUser = Annotated[User, Depends(admin_user)]


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return templates.TemplateResponse(request, "error.html", {"message": exc.detail}, status_code=exc.status_code)


def page(request: Request, name: str, user: User, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def bot_of(request: Request) -> Bot | None:
    return getattr(request.app.state, "bot", None)


# ---------- Оболочка ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/ui/me", response_class=HTMLResponse)
async def me(request: Request, user: CurrentUser):
    return page(request, "nav.html", user)


# ---------- Тикеты ----------

@app.get("/ui/tickets", response_class=HTMLResponse)
async def tickets_list(request: Request, user: CurrentUser, session: Session,
                       status: str = "", category_id: str = "", priority: str = ""):
    cat = int(category_id) if category_id.isdigit() else None
    items = await svc.list_tickets(session, status=status or None, category_id=cat, priority=priority or None)
    categories = list(await session.scalars(select(Category).order_by(Category.name)))
    return page(request, "tickets.html", user, tickets=items, categories=categories,
                f={"status": status, "category_id": cat, "priority": priority})


async def _ticket_page(request: Request, user: User, session: AsyncSession, ticket_id: int,
                       notice: str = "", error: str = "") -> HTMLResponse:
    ticket = await svc.get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Тикет не найден")
    ctx = ticket_context(ticket)
    reply_templates = [(t.name, render(t.text, ctx))
                       for t in await session.scalars(select(ReplyTemplate).order_by(ReplyTemplate.name))]
    email_templates = list(await session.scalars(select(EmailTemplate).order_by(EmailTemplate.name)))
    return page(request, "ticket.html", user, ticket=ticket, reply_templates=reply_templates,
                email_templates=email_templates, mail_enabled=get_settings().mail_enabled,
                notice=notice, error=error)


@app.get("/ui/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail(request: Request, ticket_id: int, user: CurrentUser, session: Session):
    return await _ticket_page(request, user, session, ticket_id)


async def _load(session: AsyncSession, ticket_id: int):
    ticket = await svc.get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Тикет не найден")
    return ticket


async def _refresh_card_safe(request: Request, session: AsyncSession, ticket) -> str:
    bot = bot_of(request)
    if bot is None:
        return ""
    try:
        await refresh_card(bot, session, ticket)
    except Exception as e:  # Telegram недоступен — изменения в БД всё равно сохраняем
        log.warning("refresh_card: %s", e)
        return "Не удалось обновить карточку в Telegram"
    return ""


@app.post("/ui/tickets/{ticket_id}/status", response_class=HTMLResponse)
async def ticket_status(request: Request, ticket_id: int, user: CurrentUser, session: Session,
                        status: Annotated[str, Form()]):
    ticket = await _load(session, ticket_id)
    try:
        await svc.set_status(session, ticket, status, user)
    except svc.TransitionError as e:
        return await _ticket_page(request, user, session, ticket_id, error=str(e))
    error = await _refresh_card_safe(request, session, ticket)
    await session.commit()
    return await _ticket_page(request, user, session, ticket_id, notice="Статус изменён", error=error)


@app.post("/ui/tickets/{ticket_id}/priority", response_class=HTMLResponse)
async def ticket_priority(request: Request, ticket_id: int, user: CurrentUser, session: Session,
                          priority: Annotated[str, Form()]):
    ticket = await _load(session, ticket_id)
    try:
        await svc.set_priority(session, ticket, priority)
    except ValueError as e:
        return await _ticket_page(request, user, session, ticket_id, error=str(e))
    error = await _refresh_card_safe(request, session, ticket)
    await session.commit()
    return await _ticket_page(request, user, session, ticket_id, notice="Приоритет изменён", error=error)


@app.post("/ui/tickets/{ticket_id}/reply", response_class=HTMLResponse)
async def ticket_reply(request: Request, ticket_id: int, user: CurrentUser, session: Session,
                       text: Annotated[str, Form()], visible: Annotated[str, Form()] = ""):
    ticket = await _load(session, ticket_id)
    text = text.strip()
    if not text:
        return await _ticket_page(request, user, session, ticket_id, error="Пустой ответ")
    msg = await svc.add_operator_message(session, ticket, operator=user, text=text, kind=Kind.INTERNAL)
    await session.commit()
    notice, error = "Комментарий сохранён", ""
    if visible:
        bot = bot_of(request)
        try:
            if bot is None:
                raise RuntimeError("BOT_TOKEN не задан")
            await publish_to_client(bot, session, ticket, msg)
            await session.commit()
            notice = "Ответ отправлен клиенту"
        except Exception as e:
            log.warning("publish_to_client: %s", e)
            error = f"Не удалось отправить клиенту, сохранено как внутренний комментарий: {e}"
    return await _ticket_page(request, user, session, ticket_id, notice=notice, error=error)


@app.get("/ui/tickets/{ticket_id}/email-form", response_class=HTMLResponse)
async def ticket_email_form(request: Request, ticket_id: int, user: CurrentUser, session: Session,
                            template_id: str = "", to: str = ""):
    ticket = await _load(session, ticket_id)
    subject = body = ""
    if template_id.isdigit():
        tpl = await session.get(EmailTemplate, int(template_id))
        if tpl is not None:
            ctx = ticket_context(ticket)
            subject, body = render(tpl.subject, ctx), render(tpl.body, ctx)
    email_templates = list(await session.scalars(select(EmailTemplate).order_by(EmailTemplate.name)))
    return page(request, "email_form.html", user, ticket=ticket, email_templates=email_templates,
                template_id=int(template_id) if template_id.isdigit() else None,
                to=to, subject=subject, body=body)


@app.post("/ui/tickets/{ticket_id}/email", response_class=HTMLResponse)
async def ticket_email(request: Request, ticket_id: int, user: CurrentUser, session: Session,
                       to: Annotated[str, Form()], subject: Annotated[str, Form()],
                       body: Annotated[str, Form()]):
    ticket = await _load(session, ticket_id)
    if "@" not in to:
        return await _ticket_page(request, user, session, ticket_id, error="Укажите адрес получателя")
    try:
        await send_ticket_email(session, get_settings(), ticket, to=to.strip(), subject=subject, body=body,
                                operator=user)
        await session.commit()
    except Exception as e:
        log.warning("send_ticket_email: %s", e)
        await session.rollback()
        return await _ticket_page(request, user, session, ticket_id, error=f"Письмо не отправлено: {e}")
    return await _ticket_page(request, user, session, ticket_id, notice="Письмо отправлено")


@app.get("/files/{attachment_id}")
async def file_download(request: Request, attachment_id: int, user: CurrentUser, session: Session):
    att = await session.get(Attachment, attachment_id)
    bot = bot_of(request)
    if att is None or bot is None:
        raise HTTPException(404, "Файл не найден")
    try:
        tg_file = await bot.get_file(att.tg_file_id)
        data = await bot.download_file(tg_file.file_path)
    except Exception as e:
        raise HTTPException(502, f"Не удалось получить файл из Telegram: {e}")
    media = att.mime or ("image/jpeg" if att.file_type == "photo" else "application/octet-stream")
    return Response(data.read(), media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{att.file_name or "file"}"'})


# ---------- Справочники (только админ) ----------

async def _chats_page(request: Request, user: User, session: AsyncSession, error: str = ""):
    chats = list(await session.scalars(select(Chat).order_by(Chat.is_support_chat.desc(), Chat.title)))
    categories = list(await session.scalars(select(Category).order_by(Category.name)))
    return page(request, "settings_chats.html", user, chats=chats, categories=categories, error=error)


@app.get("/ui/settings/chats", response_class=HTMLResponse)
async def chats_page(request: Request, user: AdminUser, session: Session):
    return await _chats_page(request, user, session)


@app.post("/ui/settings/chats/{chat_id}", response_class=HTMLResponse)
async def chat_update(request: Request, chat_id: int, user: AdminUser, session: Session,
                      category_id: Annotated[str, Form()] = ""):
    chat = await session.get(Chat, chat_id)
    if chat is not None and not chat.is_support_chat:
        chat.category_id = int(category_id) if category_id.isdigit() else None
        await session.commit()
    return await _chats_page(request, user, session)


@app.post("/ui/settings/chats/{chat_id}/delete", response_class=HTMLResponse)
async def chat_delete(request: Request, chat_id: int, user: AdminUser, session: Session):
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        await session.delete(chat)
        await session.commit()
    return await _chats_page(request, user, session)


@app.post("/ui/settings/categories", response_class=HTMLResponse)
async def category_create(request: Request, user: AdminUser, session: Session, name: Annotated[str, Form()]):
    if name.strip():
        await svc.get_or_create_category(session, name)
        await session.commit()
    return await _chats_page(request, user, session)


@app.post("/ui/settings/categories/{cat_id}", response_class=HTMLResponse)
async def category_rename(request: Request, cat_id: int, user: AdminUser, session: Session,
                          name: Annotated[str, Form()]):
    cat = await session.get(Category, cat_id)
    error = ""
    if cat is not None and name.strip():
        cat.name = name.strip()
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            error = "Категория с таким названием уже есть"
    return await _chats_page(request, user, session, error=error)


@app.post("/ui/settings/categories/{cat_id}/delete", response_class=HTMLResponse)
async def category_delete(request: Request, cat_id: int, user: AdminUser, session: Session):
    cat = await session.get(Category, cat_id)
    if cat is not None:
        await session.delete(cat)
        await session.commit()
    return await _chats_page(request, user, session)


# Шаблоны быстрых ответов и писем — одинаковый CRUD
_TEMPLATE_KINDS = {
    "reply-templates": (ReplyTemplate, "Шаблоны быстрых ответов"),
    "email-templates": (EmailTemplate, "Шаблоны писем"),
}


def _template_model(kind: str):
    if kind not in _TEMPLATE_KINDS:
        raise HTTPException(404, "Раздел не найден")
    return _TEMPLATE_KINDS[kind]


async def _templates_page(request: Request, user: User, session: AsyncSession, kind: str, error: str = ""):
    model, title = _template_model(kind)
    items = list(await session.scalars(select(model).order_by(model.name)))
    return page(request, "settings_templates.html", user, items=items, kind=kind, title=title,
                is_email=model is EmailTemplate, error=error)


@app.get("/ui/settings/{kind}", response_class=HTMLResponse)
async def templates_page(request: Request, kind: str, user: AdminUser, session: Session):
    return await _templates_page(request, user, session, kind)


@app.post("/ui/settings/{kind}", response_class=HTMLResponse)
async def template_create(request: Request, kind: str, user: AdminUser, session: Session,
                          name: Annotated[str, Form()], text: Annotated[str, Form()] = "",
                          subject: Annotated[str, Form()] = ""):
    model, _ = _template_model(kind)
    if not name.strip():
        return await _templates_page(request, user, session, kind, error="Укажите название")
    obj = model(name=name.strip())
    _fill_template(obj, text, subject)
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _templates_page(request, user, session, kind, error="Шаблон с таким названием уже есть")
    return await _templates_page(request, user, session, kind)


def _fill_template(obj, text: str, subject: str) -> None:
    if isinstance(obj, EmailTemplate):
        obj.subject, obj.body = subject, text
    else:
        obj.text = text


@app.post("/ui/settings/{kind}/{item_id}", response_class=HTMLResponse)
async def template_update(request: Request, kind: str, item_id: int, user: AdminUser, session: Session,
                          name: Annotated[str, Form()], text: Annotated[str, Form()] = "",
                          subject: Annotated[str, Form()] = ""):
    model, _ = _template_model(kind)
    obj = await session.get(model, item_id)
    if obj is not None and name.strip():
        obj.name = name.strip()
        _fill_template(obj, text, subject)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await _templates_page(request, user, session, kind, error="Шаблон с таким названием уже есть")
    return await _templates_page(request, user, session, kind)


@app.post("/ui/settings/{kind}/{item_id}/delete", response_class=HTMLResponse)
async def template_delete(request: Request, kind: str, item_id: int, user: AdminUser, session: Session):
    model, _ = _template_model(kind)
    obj = await session.get(model, item_id)
    if obj is not None:
        await session.delete(obj)
        await session.commit()
    return await _templates_page(request, user, session, kind)


# Операторы
async def _users_page(request: Request, user: User, session: AsyncSession, error: str = ""):
    staff = list(await session.scalars(select(User).where(User.role.in_(Role.STAFF)).order_by(User.name)))
    return page(request, "settings_users.html", user, staff=staff, error=error)


@app.get("/ui/users", response_class=HTMLResponse)
async def users_page(request: Request, user: AdminUser, session: Session):
    return await _users_page(request, user, session)


@app.post("/ui/users", response_class=HTMLResponse)
async def user_add(request: Request, user: AdminUser, session: Session,
                   tg_id: Annotated[str, Form()], name: Annotated[str, Form()] = "",
                   role: Annotated[str, Form()] = Role.OPERATOR):
    if not tg_id.strip().lstrip("-").isdigit() or role not in Role.STAFF:
        return await _users_page(request, user, session, error="Укажите числовой Telegram ID")
    u = await svc.get_or_create_user(session, int(tg_id), name.strip())
    u.role = role
    await session.commit()
    return await _users_page(request, user, session)


@app.post("/ui/users/{user_id}", response_class=HTMLResponse)
async def user_update(request: Request, user_id: int, user: AdminUser, session: Session,
                      role: Annotated[str, Form()]):
    u = await session.get(User, user_id)
    if u is None or role not in Role.LABELS:
        raise HTTPException(404, "Пользователь не найден")
    if u.id == user.id and role != Role.ADMIN:
        return await _users_page(request, user, session, error="Нельзя снять права админа с самого себя")
    u.role = role
    await session.commit()
    return await _users_page(request, user, session)
