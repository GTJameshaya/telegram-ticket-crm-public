"""Настройки приложения. Все секреты берутся только из переменных окружения / .env."""
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://crm:crm@db:5432/crm"

    # Домен Mini App, например 203.0.113.10.sslip.io (без https://)
    webapp_domain: str = ""

    # Telegram ID админов через запятую: "111,222"
    admin_tg_ids: Annotated[list[int], NoDecode] = []

    # Почта. Транспорт выбирается автоматически (см. send_transport / receive_transport):
    #  1) GMAIL_APP_PASSWORD задан → приём IMAP, отправка SMTP (или SMTP-relay, если задан SMTP_HOST);
    #  2) иначе GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN → Gmail API по HTTPS (scripts/gmail_auth.py).
    gmail_user: str = ""
    gmail_app_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587          # 587 — STARTTLS, 465 — SSL, другие (например 2525 у relay) — STARTTLS, если есть
    smtp_user: str = ""           # логин SMTP-relay; для Gmail по умолчанию GMAIL_USER
    smtp_password: str = ""       # пароль SMTP-relay; для Gmail по умолчанию GMAIL_APP_PASSWORD
    smtp_timeout: int = 15
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    # Интервал опроса почты, секунд. Старое имя IMAP_POLL_SECONDS тоже принимается.
    mail_poll_seconds: int = Field(90, validation_alias=AliasChoices("MAIL_POLL_SECONDS", "IMAP_POLL_SECONDS"))

    # Токен HTTP API приёма тикетов (POST /api/ingest/ticket). Пусто — API выключен (404).
    ingest_token: str = ""

    # Часовой пояс для отображения дат в Mini App
    timezone: str = "Europe/Moscow"

    # Срок жизни initData Mini App, секунд
    webapp_auth_max_age: int = 86400

    @field_validator("smtp_host", "smtp_port", "smtp_timeout", "imap_host", "imap_port", mode="before")
    @classmethod
    def _empty_is_default(cls, v, info):
        # Пустое значение из .env / docker compose (${SMTP_PORT:-}) = значение по умолчанию
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("admin_tg_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def webapp_url(self) -> str:
        return f"https://{self.webapp_domain}/" if self.webapp_domain else ""

    # ---------- почта ----------

    @property
    def gmail_api_missing_vars(self) -> list[str]:
        return [name for name, value in (
            ("GMAIL_USER", self.gmail_user),
            ("GMAIL_CLIENT_ID", self.gmail_client_id),
            ("GMAIL_CLIENT_SECRET", self.gmail_client_secret),
            ("GMAIL_REFRESH_TOKEN", self.gmail_refresh_token),
        ) if not (value or "").strip()]

    @property
    def gmail_api_configured(self) -> bool:
        return not self.gmail_api_missing_vars

    @property
    def app_password_configured(self) -> bool:
        return bool(self.gmail_user.strip() and self.gmail_app_password.strip())

    @property
    def smtp_is_gmail(self) -> bool:
        return self.smtp_host.strip().lower() in ("smtp.gmail.com", "smtp.googlemail.com")

    @property
    def smtp_relay_configured(self) -> bool:
        """Задан сторонний SMTP (relay): свой SMTP_HOST или свои SMTP_USER/SMTP_PASSWORD."""
        return bool(self.gmail_user.strip()) and (
            not self.smtp_is_gmail or bool(self.smtp_user.strip() or self.smtp_password.strip()))

    @property
    def smtp_credentials(self) -> tuple[str, str] | None:
        """(логин, пароль) для SMTP AUTH или None — без авторизации."""
        if self.smtp_user.strip() or self.smtp_password.strip():
            return (self.smtp_user.strip() or self.gmail_user.strip(), self.smtp_password)
        if self.smtp_is_gmail and self.app_password_configured:
            return (self.gmail_user.strip(), self.gmail_app_password)
        return None

    @property
    def send_transport(self) -> str | None:
        """smtp | gmail_api | None."""
        if self.app_password_configured or self.smtp_relay_configured:
            return "smtp"
        if self.gmail_api_configured:
            return "gmail_api"
        return None

    @property
    def receive_transport(self) -> str | None:
        """imap | gmail_api | None."""
        if self.app_password_configured:
            return "imap"
        if self.gmail_api_configured:
            return "gmail_api"
        return None

    @property
    def mail_enabled(self) -> bool:
        """Можно отправлять письма из Mini App."""
        return self.send_transport is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
