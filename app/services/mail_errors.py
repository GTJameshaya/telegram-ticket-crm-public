"""Общие ошибки почтовых транспортов (тексты — по-русски, показываются в Mini App)."""
from app.config import Settings


class MailError(RuntimeError):
    """Ошибка отправки/приёма почты с понятным текстом для UI и логов."""


def not_configured_message(settings: Settings) -> str:
    return ("Почта не настроена: задайте в .env GMAIL_USER и GMAIL_APP_PASSWORD (SMTP/IMAP) "
            "либо GMAIL_USER, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN (Gmail API) — "
            "см. README, раздел «Почта Gmail»")
