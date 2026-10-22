"""Mail-worker: раз в MAIL_POLL_SECONDS проверяет почту (IMAP или Gmail API) и прикрепляет ответы к тикетам.

python -m app.mail_worker.main
"""
import asyncio
import logging

from aiogram import Bot

from app.chat_migration import install as install_migration
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.services import gmail, smtp_imap
from app.services.mail_errors import MailError, not_configured_message
from app.services.mail import parse_email, process_incoming
from app.telegram_ops import notify_new_message

log = logging.getLogger(__name__)


def _is_own(settings: Settings, from_addr: str, labels: list[str]) -> bool:
    return "SENT" in labels or from_addr.lower() == settings.gmail_user.strip().lower()


async def poll_once(settings: Settings, bot: Bot) -> int:
    """Одна итерация опроса. Возвращает число писем, прикреплённых к тикетам."""
    if settings.receive_transport == "imap":
        fetch, mark = smtp_imap.fetch_unseen, smtp_imap.mark_seen
    else:
        fetch, mark = gmail.fetch_unread, gmail.mark_read
    messages = await asyncio.to_thread(fetch, settings)
    attached, done = 0, []
    for gid, raw, labels in messages:
        try:
            inc = parse_email(raw)
        except Exception:
            log.exception("Не удалось разобрать письмо %s — помечаем прочитанным", gid)
            done.append(gid)
            continue
        if _is_own(settings, inc.from_addr, labels):
            done.append(gid)
            continue
        async with get_sessionmaker()() as session:
            result = await process_incoming(session, inc)
            if result is not None:
                ticket, msg, reopened = result
                await session.commit()
                try:
                    await notify_new_message(bot, session, ticket, msg, reopened=reopened)
                    await session.commit()
                except Exception:
                    log.exception("Не удалось уведомить CRM-чат о письме в тикете #%s", ticket.number)
                attached += 1
                log.info("Письмо %s прикреплено к тикету #%s", inc.message_id, ticket.number)
            else:
                # Письма без метки [#N] не трогаем — помечаем прочитанными, чтобы не разбирать повторно.
                log.info("Письмо %s («%s») не относится к тикетам", inc.message_id, inc.subject)
        done.append(gid)
    await asyncio.to_thread(mark, settings, done)
    return attached


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if settings.receive_transport is None:
        log.warning("%s — mail-worker простаивает", not_configured_message(settings))
        while True:
            await asyncio.sleep(3600)
    bot = install_migration(Bot(settings.bot_token))
    name = {"imap": f"IMAP {settings.imap_host}:{settings.imap_port}", "gmail_api": "Gmail API"}
    log.info("Mail-worker запущен (%s, %s), интервал %s с; отправка: %s", name[settings.receive_transport],
             settings.gmail_user, settings.mail_poll_seconds, settings.send_transport or "не настроена")
    while True:
        try:
            await poll_once(settings, bot)
        except MailError as e:
            log.error("Ошибка опроса почты: %s", e)
        except Exception:
            log.exception("Ошибка опроса почты")
        await asyncio.sleep(settings.mail_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
