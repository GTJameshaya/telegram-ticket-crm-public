"""Транспорт Gmail API поверх HTTPS (OAuth, refresh token) — отправка и приём писем без SMTP/IMAP.

Все функции здесь синхронные (google-api-python-client блокирующий) — вызывайте их через
asyncio.to_thread(). Сервис Gmail создаётся функцией get_service(); в тестах её подменяют фейком.
"""
from __future__ import annotations

import base64
import logging
import threading

from app.config import Settings
from app.services.mail_errors import MailError, not_configured_message  # noqa: F401

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
UNREAD_QUERY = "is:unread in:inbox"


class GmailError(MailError):
    """Ошибка Gmail API с понятным (русским) текстом для UI и логов."""


# Credentials кэшируем, чтобы не обновлять access token на каждый вызов. Сам сервис (httplib2)
# не потокобезопасен, поэтому на каждую операцию строим новый — это дёшево (discovery встроен в пакет).
_creds_lock = threading.Lock()
_creds_cache: dict[tuple, object] = {}


def _credentials(settings: Settings):
    from google.oauth2.credentials import Credentials

    key = (settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_refresh_token)
    with _creds_lock:
        creds = _creds_cache.get(key)
        if creds is None:
            creds = Credentials(
                token=None,
                refresh_token=settings.gmail_refresh_token,
                client_id=settings.gmail_client_id,
                client_secret=settings.gmail_client_secret,
                token_uri=TOKEN_URI,
                scopes=SCOPES,
            )
            _creds_cache.clear()
            _creds_cache[key] = creds
        return creds


def get_service(settings: Settings):
    """Клиент Gmail API v1 (users.*). Бросает GmailError, если почта не настроена."""
    if not settings.gmail_api_configured:
        missing = ", ".join(settings.gmail_api_missing_vars)
        raise GmailError(f"Gmail API не настроен: в .env не заданы {missing}")
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(settings), cache_discovery=False)


def _friendly(e: Exception) -> GmailError:
    """Превращает ошибки google-auth / googleapiclient в GmailError с русским текстом."""
    if isinstance(e, GmailError):
        return e
    try:
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover
        return GmailError(f"Ошибка Gmail: {e}")
    if isinstance(e, RefreshError):
        return GmailError(
            "Gmail отклонил авторизацию (refresh token недействителен, отозван или истёк — "
            "приложение OAuth в режиме Testing выдаёт токены на 7 дней). "
            f"Получите новый токен: python scripts/gmail_auth.py. Детали: {e}"
        )
    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", "?")
        reason = e._get_reason() if hasattr(e, "_get_reason") else str(e)
        return GmailError(f"Gmail API вернул ошибку {status}: {reason}")
    return GmailError(f"Ошибка Gmail: {e}")


def _call(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        raise _friendly(e) from e


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ---------- Отправка ----------

def send_raw(settings: Settings, raw: bytes, *, service=None) -> dict:
    """users.messages.send. Возвращает ответ Gmail ({'id', 'threadId', 'labelIds'})."""
    service = service or _call(lambda: get_service(settings))
    return _call(lambda: service.users().messages().send(
        userId="me", body={"raw": b64url_encode(raw)}).execute())


def get_message_id_header(settings: Settings, gmail_id: str, *, service=None) -> str:
    """Заголовок Message-ID сохранённого в Gmail письма ('' если не найден)."""
    service = service or _call(lambda: get_service(settings))
    msg = _call(lambda: service.users().messages().get(
        userId="me", id=gmail_id, format="metadata", metadataHeaders=["Message-ID"]).execute())
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == "message-id":
            return (h.get("value") or "").strip()
    return ""


# ---------- Приём ----------

def list_unread_ids(settings: Settings, *, service=None, limit: int = 500) -> list[str]:
    """ID непрочитанных писем во «Входящих» (от старых к новым)."""
    service = service or _call(lambda: get_service(settings))
    ids: list[str] = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": UNREAD_QUERY, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _call(lambda: service.users().messages().list(**kwargs).execute())
        ids.extend(m["id"] for m in resp.get("messages") or [])
        page_token = resp.get("nextPageToken")
        if not page_token or len(ids) >= limit:
            break
    ids = ids[:limit]
    ids.reverse()  # Gmail отдаёт новые первыми — обрабатываем в хронологическом порядке
    return ids


def get_raw(settings: Settings, gmail_id: str, *, service=None) -> tuple[bytes, list[str]]:
    """Письмо целиком (RFC 822, format=raw) и его метки Gmail."""
    service = service or _call(lambda: get_service(settings))
    msg = _call(lambda: service.users().messages().get(userId="me", id=gmail_id, format="raw").execute())
    return b64url_decode(msg.get("raw") or ""), list(msg.get("labelIds") or [])


def fetch_unread(settings: Settings) -> list[tuple[str, bytes, list[str]]]:
    """[(gmail_id, raw, labelIds)] непрочитанных писем во «Входящих» (не помечая их прочитанными)."""
    service = _call(lambda: get_service(settings))
    out = []
    for gid in list_unread_ids(settings, service=service):
        try:
            raw, labels = get_raw(settings, gid, service=service)
        except GmailError:
            log.exception("Не удалось получить письмо %s — попробуем в следующий раз", gid)
            continue
        out.append((gid, raw, labels))
    return out


def mark_read(settings: Settings, gmail_ids: list[str], *, service=None) -> None:
    """Снимает метку UNREAD (users.messages.batchModify, до 1000 id за запрос)."""
    if not gmail_ids:
        return
    service = service or _call(lambda: get_service(settings))
    for i in range(0, len(gmail_ids), 1000):
        chunk = gmail_ids[i:i + 1000]
        _call(lambda: service.users().messages().batchModify(
            userId="me", body={"ids": chunk, "removeLabelIds": ["UNREAD"]}).execute())
