#!/usr/bin/env python3
"""Получение OAuth refresh token для Gmail API (один раз, на своём компьютере с браузером).

Использование:
    python scripts/gmail_auth.py путь/к/client_secret_XXX.json gmail.env

1. Откроется браузер: войдите в ящик GMAIL_USER и разрешите доступ (gmail.modify).
2. В файл gmail.env будут записаны строки GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN
   в формате .env — перенесите их в .env на сервере. Секреты в консоль не выводятся.

Нужен пакет google-auth-oauthlib:  pip install google-auth-oauthlib
"""
import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        print("Ошибка: нужно 2 аргумента — путь к JSON OAuth-клиента (Desktop app) и путь к файлу результата.",
              file=sys.stderr)
        return 2
    client_json, out_path = argv[1], argv[2]
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Не установлен google-auth-oauthlib: pip install google-auth-oauthlib", file=sys.stderr)
        return 2

    with open(client_json, encoding="utf-8") as f:
        info = json.load(f)
    if "installed" not in info:
        print("Внимание: ожидается OAuth-клиент типа «Desktop app» (ключ \"installed\" в JSON).", file=sys.stderr)

    flow = InstalledAppFlow.from_client_secrets_file(client_json, scopes=SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Откройте в браузере, если он не открылся сам:\n{url}\n",
        success_message="Готово! Окно можно закрыть и вернуться в терминал.",
    )
    if not creds.refresh_token:
        print("Google не вернул refresh token. Отзовите доступ на https://myaccount.google.com/permissions "
              "и запустите скрипт ещё раз.", file=sys.stderr)
        return 1

    email = ""
    try:
        from googleapiclient.discovery import build

        profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(
            userId="me").execute()
        email = profile.get("emailAddress", "")
    except Exception as e:  # noqa: BLE001 — не критично, просто не узнаем адрес
        print(f"(не удалось узнать адрес ящика: {e})", file=sys.stderr)

    lines = []
    if email:
        lines.append(f"GMAIL_USER={email}")
    lines += [
        f"GMAIL_CLIENT_ID={creds.client_id}",
        f"GMAIL_CLIENT_SECRET={creds.client_secret}",
        f"GMAIL_REFRESH_TOKEN={creds.refresh_token}",
    ]
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Refresh token получен{' для ' + email if email else ''}. "
          f"Строки для .env записаны в {out_path} (права 600) — перенесите их в .env на сервере.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
