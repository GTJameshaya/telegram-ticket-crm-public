"""Транспорт SMTP (отправка) + IMAP (приём) — Gmail с паролем приложения или любой SMTP-relay.

Функции синхронные — вызывайте через asyncio.to_thread().
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage

from app.config import Settings
from app.services.mail_errors import MailError

log = logging.getLogger(__name__)


class SmtpBlockedError(MailError):
    """Нет TCP-соединения с SMTP-сервером (типично: хостинг блокирует исходящие SMTP-порты)."""


def _blocked_message(host: str, port: int) -> str:
    return (f"Отправка писем недоступна: SMTP-порты заблокированы хостингом (нет соединения с {host}:{port}). "
            "Приём писем продолжает работать. Чтобы отправлять, укажите SMTP-relay на другом порту "
            "(SMTP_HOST / SMTP_PORT, например 2525, SMTP_USER / SMTP_PASSWORD) или используйте Gmail API.")


def _connect_smtp(settings: Settings) -> smtplib.SMTP:
    host, port, timeout = settings.smtp_host.strip(), settings.smtp_port, settings.smtp_timeout
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        smtp = smtplib.SMTP(host, port, timeout=timeout)
    except socket.gaierror as e:
        raise MailError(f"Отправка писем недоступна: не найден SMTP-сервер {host} ({e})") from e
    except (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as e:
        raise SmtpBlockedError(_blocked_message(host, port)) from e
    try:
        smtp.ehlo()
        if smtp.has_extn("starttls"):
            smtp.starttls(context=ctx)
            smtp.ehlo()
        elif settings.smtp_credentials is not None:
            raise MailError(f"SMTP-сервер {host}:{port} не поддерживает STARTTLS — пароль по открытому каналу "
                            "не отправляем. Для SSL укажите SMTP_PORT=465.")
    except BaseException:
        smtp.close()
        raise
    return smtp


def smtp_send(settings: Settings, msg: EmailMessage) -> str:
    """Отправляет письмо по SMTP. Возвращает Message-ID (SMTP его не переписывает)."""
    try:
        smtp = _connect_smtp(settings)
        try:
            creds = settings.smtp_credentials
            if creds is not None:
                smtp.login(*creds)
            smtp.send_message(msg, from_addr=settings.gmail_user.strip())
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                smtp.close()
    except MailError as e:
        log.warning("SMTP: %s", e)
        raise
    except smtplib.SMTPAuthenticationError as e:
        log.warning("SMTP auth error: %s", e)
        raise MailError("SMTP-сервер отклонил логин/пароль. Для Gmail нужен пароль приложения "
                        "(GMAIL_APP_PASSWORD), для relay — SMTP_USER / SMTP_PASSWORD.") from e
    except smtplib.SMTPRecipientsRefused as e:
        raise MailError(f"SMTP-сервер отклонил адрес получателя: {', '.join(e.recipients)}") from e
    except (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected) as e:
        log.warning("SMTP connection error: %s", e)
        raise SmtpBlockedError(_blocked_message(settings.smtp_host.strip(), settings.smtp_port)) from e
    except (smtplib.SMTPException, OSError) as e:
        log.warning("SMTP error: %s", e)
        raise MailError(f"Ошибка SMTP: {e}") from e
    return str(msg["Message-ID"])


# ---------- IMAP ----------

def _imap(settings: Settings) -> imaplib.IMAP4_SSL:
    try:
        imap = imaplib.IMAP4_SSL(settings.imap_host.strip(), settings.imap_port, timeout=30)
    except OSError as e:
        raise MailError(f"Нет соединения с IMAP {settings.imap_host}:{settings.imap_port}: {e}") from e
    try:
        imap.login(settings.gmail_user.strip(), settings.gmail_app_password.strip())
    except imaplib.IMAP4.error as e:
        imap.shutdown()
        raise MailError("IMAP: Gmail отклонил логин/пароль приложения (GMAIL_USER / GMAIL_APP_PASSWORD); "
                        f"проверьте, что IMAP включён. Детали: {e}") from e
    return imap


def fetch_unseen(settings: Settings) -> list[tuple[bytes, bytes, list[str]]]:
    """[(uid, raw, [])] непрочитанных писем во «Входящих» (BODY.PEEK — не помечая прочитанными)."""
    imap = _imap(settings)
    try:
        imap.select("INBOX")
        typ, data = imap.uid("search", None, "UNSEEN")
        out = []
        for uid in (data[0] or b"").split() if typ == "OK" and data else []:
            typ, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple):
                out.append((uid, msg_data[0][1], []))
        return out
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def mark_seen(settings: Settings, uids: list[bytes]) -> None:
    if not uids:
        return
    imap = _imap(settings)
    try:
        imap.select("INBOX")
        for uid in uids:
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
