"""Проверка Telegram WebApp initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Возвращает hash для набора полей (используется в тестах и для отладки)."""
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return hmac.new(_secret_key(bot_token), data_check.encode(), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400, now: float | None = None) -> dict:
    """Проверяет подпись initData и возвращает словарь пользователя Telegram (id, first_name, ...)."""
    if not init_data or not bot_token:
        raise InitDataError("Нет данных авторизации")
    fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = fields.pop("hash", None)
    if not received:
        raise InitDataError("Нет подписи")
    if not hmac.compare_digest(sign_init_data(fields, bot_token), received):
        raise InitDataError("Неверная подпись")
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("Неверная дата авторизации")
    if max_age and (now or time.time()) - auth_date > max_age:
        raise InitDataError("Данные авторизации устарели, откройте CRM заново")
    try:
        user = json.loads(fields.get("user", ""))
    except json.JSONDecodeError:
        raise InitDataError("Нет данных пользователя")
    if not isinstance(user, dict) or "id" not in user:
        raise InitDataError("Нет данных пользователя")
    return user
