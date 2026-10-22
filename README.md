# telegram-ticket-crm

CRM тикетов в Telegram: бот в рабочих чатах + служебный CRM-чат + веб-панель в виде Telegram Mini App.
Техническое задание — [docs/SPEC.md](docs/SPEC.md).

Сообщения из рабочих Telegram-чатов, письма из Gmail и запросы от внешних систем (HTTP API) превращаются
в тикеты; операторы ведут их в отдельном CRM-чате и в Mini App и отвечают клиентам обратно в чат или по почте.

## Стек

- Python 3.12, [aiogram 3](https://docs.aiogram.dev/) (бот, long polling)
- FastAPI + Jinja2 + HTMX (Telegram Mini App), авторизация по подписи `initData`
- PostgreSQL 16, SQLAlchemy 2 (async, asyncpg), Alembic (миграции)
- Почта: IMAP/SMTP (пароль приложения Gmail или SMTP-relay) либо Gmail API (OAuth)
- Docker Compose + Caddy (автоматический HTTPS Let's Encrypt; домен без покупки — `<IP>.sslip.io`)
- pytest (SQLite в памяти, фейковый Telegram API)

## Быстрый старт

Нужны: сервер с публичным IP и Docker (порты 80/443 открыты), бот от @BotFather, ваш Telegram ID.

```bash
git clone https://github.com/GTJameshaya/telegram-ticket-crm-public.git
cd telegram-ticket-crm-public
cp .env.example .env    # заполните BOT_TOKEN, ADMIN_TG_IDS, POSTGRES_PASSWORD, DATABASE_URL, WEBAPP_DOMAIN
docker compose up -d --build
```

Затем назначьте CRM-чат и зарегистрируйте рабочие чаты (раздел [«Регистрация чатов»](#4-регистрация-чатов)).
Подробности — ниже.

## Что умеет

- **Создание тикета.** Участник рабочего чата пишет `@бот текст` (можно с фото, документами, видео, голосовыми) →
  создаётся тикет `#N`, категория = чат, откуда пришло сообщение. Бот отвечает «Создан тикет #N».
- **Карточка в CRM-чате**: «Новое сообщение в тикете #N», категория, приоритет, статус, автор, текст и кнопки
  «Взять в работу» / «Отложить» / «Закрыть». Вложения копируются следом. При смене статуса карточка
  редактируется, а не дублируется.
- **Привязка сообщений.** Реплай на любое сообщение тикета (исходное, «Создан тикет», ответ оператора) попадает
  в тот же тикет, в CRM-чат приходит «Новое сообщение в тикете #N». Упоминание бота вне ветки — новый тикет.
- **Статусы**: Новый → В работе → Отложен ↔ В работе → Закрыт. Новое сообщение в закрытом тикете
  переоткрывает его («В работе»).
- **Приоритеты**: Низкий / Обычный / Высокий / Срочный (в Mini App или `/priority` в CRM-чате).
- **Ответы операторов.** Реплай на карточку в CRM-чате = внутренний комментарий; под ним кнопка
  «Отправить клиенту» — публикует ответ реплаем на сообщение автора в рабочем чате.
  В Mini App — поле ответа с флажком «Видно клиенту».
- **Быстрые ответы** — шаблоны в БД, выбор в Mini App и команда `/t <имя>` в CRM-чате.
- **Почта** (отдельный Gmail): письмо по шаблону из тикета, в теме метка `[#N]`; ответы (по `In-Reply-To` /
  `References` или метке в теме) возвращаются в тикет и приходят в CRM-чат. Два транспорта на выбор:
  пароль приложения (IMAP + SMTP/SMTP-relay) или Gmail API по HTTPS (OAuth). Опрос раз в 60–120 с.
- **HTTP API** `POST /api/ingest/ticket` — тикеты от других ботов/систем (Telegram не доставляет ботам
  сообщения ботов), с файлами, метаданными и привязкой к ветке рабочего чата.
- **Mini App**: список тикетов с фильтрами (статус, категория, приоритет), карточка тикета с лентой и вложениями,
  смена статуса/приоритета, ответы, письма; для админов — справочники чатов/категорий, шаблонов ответов,
  шаблонов писем и операторов. Вход только для операторов/админов (проверка подписи `initData`).

## Архитектура

```
docker compose
├── db           PostgreSQL 16
├── web          FastAPI + Jinja2 + HTMX (Mini App), при старте применяет миграции Alembic
├── bot          aiogram 3, long polling
├── mail-worker  опрос почты (IMAP или Gmail API) → тикеты
└── caddy        HTTPS для <IP>.sslip.io (Let's Encrypt) → web:8000
```

```
app/
├── config.py            настройки из .env
├── db.py                подключение SQLAlchemy (async)
├── models.py            таблицы и справочники статусов/приоритетов/ролей
├── auth.py              проверка Telegram WebApp initData
├── telegram_ops.py      карточки, уведомления в CRM-чат, публикация ответа клиенту
├── services/tickets.py  логика тикетов: создание, привязка, статусы, переоткрытие
├── services/mail.py     шаблоны писем, метка [#N], отправка, разбор входящих
├── services/smtp_imap.py  транспорт SMTP (отправка) + IMAP (приём) — пароль приложения / relay
├── services/gmail.py    транспорт Gmail API (OAuth refresh token): send / list / get / снятие UNREAD
├── bot/                 обработчики бота
├── web/                 Mini App (шаблоны Jinja2, static); web/ingest.py — HTTP API приёма тикетов
└── mail_worker/         опрос почты
scripts/gmail_auth.py    получение OAuth refresh token для Gmail
alembic/                 миграции
tests/                   pytest
```

## 1. Настройка бота в @BotFather

1. `/newbot` → получите `BOT_TOKEN`.
2. **Privacy mode** — `/setprivacy` → выберите бота → **Disable**.
   Это нужно, чтобы бот видел реплаи на сообщения *автора* (а не только на свои) и сообщения в CRM-чате.
   С включённым privacy mode бот видит только упоминания, команды и реплаи на свои сообщения — тикеты
   создаваться будут, но часть реплаев не привяжется. После смены настройки **удалите и заново добавьте бота
   в группы** (или сделайте его админом группы) — иначе Telegram не применит изменение.
3. `/setjoingroups` → Enable (по умолчанию включено).
4. Mini App: бот сам выставляет кнопку меню «CRM» (ведёт на `https://WEBAPP_DOMAIN/`) при старте.
   Вручную: `/setmenubutton` → бот → URL `https://<IP>.sslip.io/` → название «CRM».
   Дополнительно можно создать прямую ссылку на приложение: `/newapp` (тот же URL) — тогда Mini App
   открывается по ссылке `t.me/<бот>/<app>` в том числе из групп.
5. Mini App открывается в личном чате с ботом (кнопка меню или `/start` → «Открыть CRM»).

## 2. Файл .env

```bash
cp .env.example .env
nano .env
```

| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен от BotFather |
| `ADMIN_TG_IDS` | Telegram ID админов через запятую (узнать: `/id` боту или @userinfobot) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | параметры БД (придумайте пароль) |
| `DATABASE_URL` | `postgresql+asyncpg://USER:PASSWORD@db:5432/DB` — те же значения, что выше |
| `WEBAPP_DOMAIN` | домен Mini App без `https://`: свой домен или `<IP сервера>.sslip.io` (например `203.0.113.10.sslip.io`) |
| `GMAIL_USER` | адрес ящика Gmail, от имени которого уходят письма |
| `GMAIL_APP_PASSWORD` | вариант 1: [пароль приложения](https://myaccount.google.com/apppasswords) → приём IMAP, отправка SMTP (см. [«Почта Gmail»](#почта-gmail)) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | SMTP для отправки: по умолчанию `smtp.gmail.com:587` (STARTTLS, `465` — SSL); можно указать SMTP-relay |
| `IMAP_HOST`, `IMAP_PORT` | IMAP для приёма, по умолчанию `imap.gmail.com:993` (SSL) |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | вариант 2: OAuth-доступ к Gmail API |
| `MAIL_POLL_SECONDS` | интервал опроса почты, по умолчанию 90 (старое имя `IMAP_POLL_SECONDS` тоже принимается) |
| `INGEST_TOKEN` | токен HTTP API приёма тикетов `POST /api/ingest/ticket` (см. [«HTTP API»](#http-api-приёма-тикетов)); пусто — API выключен |
| `TIMEZONE` | часовой пояс дат в Mini App, по умолчанию `Europe/Moscow` |

`.env` в `.gitignore` — секреты в репозиторий не попадают. Если почта не нужна, оставьте `GMAIL_*` пустыми:
mail-worker будет простаивать, а блок писем в Mini App скроется.

## Почта Gmail

Транспорт выбирается автоматически по `.env`:

| Что задано | Приём | Отправка |
|---|---|---|
| `GMAIL_USER` + `GMAIL_APP_PASSWORD` | IMAP `imap.gmail.com:993` (SSL) | SMTP `SMTP_HOST:SMTP_PORT` (по умолчанию `smtp.gmail.com:587`) |
| `GMAIL_USER` + `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` (без пароля приложения) | Gmail API | Gmail API (или SMTP-relay, если он задан) |
| ничего | mail-worker простаивает | в Mini App — «Почта не настроена» |

Поведение одинаковое для обоих вариантов: метка `[#N]` в теме, сохранение `Message-ID`, `In-Reply-To`/`References`
при ответе, поиск тикета по заголовкам и метке, отрезание цитаты, защита от дублей, пометка прочитанным,
переоткрытие закрытого тикета и уведомление в CRM-чат. Собственные письма ящика пропускаются.

### Вариант 1: пароль приложения (IMAP + SMTP)

1. В аккаунте Google включите двухэтапную аутентификацию и создайте
   [пароль приложения](https://myaccount.google.com/apppasswords). В настройках Gmail → «Пересылка и POP/IMAP»
   включите IMAP.
2. В `.env`: `GMAIL_USER=support@example.com`, `GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx`.
3. `SMTP_PORT`: `587` — STARTTLS (по умолчанию), `465` — SSL, любой другой порт — STARTTLS, если сервер его
   поддерживает (без TLS пароль не отправляется).

**DigitalOcean блокирует исходящие SMTP-порты (25, 465, 587).** Приём по IMAP (порт 993) при этом работает,
а отправка из Mini App завершится понятной ошибкой «Отправка писем недоступна: SMTP-порты заблокированы
хостингом» (в логе `web` — предупреждение `SMTP: …`). Варианты:

- **SMTP-relay на незаблокированном порту** (например, 2525 — его поддерживают Mailgun, SendGrid, Brevo,
  SMTP2GO и др.): `SMTP_HOST=smtp-relay.example.com`, `SMTP_PORT=2525`, `SMTP_USER=…`, `SMTP_PASSWORD=…`.
  Письма уходят с адресом `From: GMAIL_USER` (домен/адрес нужно подтвердить у relay-провайдера), ответы клиентов
  приходят в Gmail и забираются по IMAP. Relay можно комбинировать и с Gmail API для приёма.
- попросить поддержку DigitalOcean открыть SMTP;
- использовать Gmail API (вариант 2).

### Вариант 2: Gmail API + OAuth

Письма отправляются и принимаются через **Gmail API по HTTPS (порт 443)** — SMTP/IMAP не нужны. Доступ выдаётся
один раз через OAuth: вы входите в ящик в браузере, скрипт получает **refresh token**, и сервер дальше работает
сам. Область доступа — `https://www.googleapis.com/auth/gmail.modify` (отправка, чтение и пометка прочитанным).
Используется, только если `GMAIL_APP_PASSWORD` пуст.

1. **Проект и API.** В [Google Cloud Console](https://console.cloud.google.com/) создайте проект (или выберите
   существующий) и включите **Gmail API**: «APIs & Services» → «Library» → Gmail API → Enable.
2. **Google Auth Platform** ([console.cloud.google.com/auth](https://console.cloud.google.com/auth/overview)):
   - «Branding» — название приложения (например, «Ticket CRM») и e-mail поддержки;
   - «Audience» — тип **External**. Пока приложение в режиме **Testing**, добавьте ящик `GMAIL_USER`
     в **Test users** — иначе Google не даст войти (ошибка `access_denied`);
   - «Data access» — можно добавить scope `.../auth/gmail.modify` (не обязательно, скрипт запросит его сам);
   - «Clients» → «Create client» → тип **Desktop app** → скачайте JSON (`client_secret_….json`).
3. **Важно про срок жизни токена.** У приложений в режиме Testing refresh token **истекает через 7 дней** —
   почта перестанет работать (в логах mail-worker и в Mini App появится ошибка про refresh token).
   Поэтому **опубликуйте приложение**: «Audience» → «Publish app» → статус **In production**. Проходить
   верификацию Google не нужно: для личного использования со scope `gmail.modify` при входе будет предупреждение
   «Google hasn't verified this app» / «Приложение не проверено» — для владельца это нормально:
   «Advanced / Дополнительно» → «Go to … (unsafe) / Перейти на страницу …». Токен, полученный ещё в режиме
   Testing, после публикации всё равно истечёт — получите новый (шаг 4).
4. **Получите refresh token** на своём компьютере с браузером (не на дроплете):
   ```bash
   pip install google-auth-oauthlib google-api-python-client   # или: pip install -r requirements.txt
   python scripts/gmail_auth.py ~/Downloads/client_secret_XXXX.json gmail.env
   ```
   Откроется браузер — войдите в ящик `GMAIL_USER` и разрешите доступ. Скрипт запишет в `gmail.env`
   (права 600) строки `GMAIL_USER`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`;
   секреты в консоль не выводятся. Если Google не вернул refresh token — отзовите доступ приложения на
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions) и запустите скрипт снова.
5. Перенесите эти строки в `.env` на сервере, удалите `gmail.env` и JSON клиента с компьютера (или храните
   надёжно) и перезапустите: `docker compose up -d`.

Refresh token также перестаёт действовать, если отозвать доступ в настройках аккаунта Google или сменить пароль
ящика — тогда повторите шаг 4.

## 3. Деплой на сервер (например, дроплет DigitalOcean)

1. Создайте сервер/дроплет Ubuntu 24.04 (хватит 1 vCPU / 1 GB), откройте порты 22, 80, 443.
   Подойдёт любой VPS; ниже про DigitalOcean — только ограничение на исходящий SMTP.
2. Установите Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Склонируйте репозиторий и настройте `.env` (`WEBAPP_DOMAIN=<IP>.sslip.io`, например `203.0.113.10.sslip.io`):
   ```bash
   git clone https://github.com/GTJameshaya/telegram-ticket-crm-public.git
   cd telegram-ticket-crm-public
   cp .env.example .env && nano .env
   ```
4. Запустите:
   ```bash
   docker compose up -d --build
   docker compose logs -f web bot
   ```
   Caddy сам получит сертификат Let's Encrypt для `<IP>.sslip.io` (порты 80/443 должны быть доступны
   из интернета). Проверка: `https://<IP>.sslip.io/healthz` → `{"ok":true}`.
5. Обновление: `git pull && docker compose up -d --build` (миграции применятся автоматически при старте `web`).
6. Бэкап БД: `docker compose exec db pg_dump -U crm crm > backup.sql`.

## 4. Регистрация чатов

Все команды ниже — только для админов (`ADMIN_TG_IDS` или назначенных в Mini App).

1. Создайте группу для операторов (CRM-чат), добавьте туда бота и операторов, напишите:
   ```
   /set_support_chat
   ```
2. Добавьте бота в каждый рабочий чат и напишите там:
   ```
   /register_chat Показания счетчиков
   ```
   Аргумент — название категории (создаётся автоматически). Без аргумента категорией станет название чата.
   `/unregister_chat` — отключить чат. Категории и привязку чатов можно менять в Mini App («Чаты и категории»).
3. Добавьте операторов: в Mini App → «Операторы» (по Telegram ID) или командой
   `/add_operator <tg_id> [имя]` (либо реплаем `/add_operator` на сообщение человека).
   `/remove_operator <tg_id>` — убрать.

## HTTP API приёма тикетов

Telegram **не доставляет ботам сообщения других ботов**: если другой бот пишет в рабочий чат `@бот …`, CRM этого
не увидит. Поэтому внешний бот (или любая система) после отправки своего сообщения дублирует его в CRM по HTTP.
Поведение — как у упоминания бота в чате: создаётся тикет, карточка уходит в CRM-чат, в рабочий чат пишется
«Создан тикет #N», а сообщения связываются, так что реплаи людей в этой ветке попадают в тикет, а публичные
ответы операторов уходят в тот же чат.

```
POST https://<WEBAPP_DOMAIN>/api/ingest/ticket
Authorization: Bearer <INGEST_TOKEN>
Content-Type: multipart/form-data
```

Если `INGEST_TOKEN` не задан — эндпоинт выключен (404). Токен сравнивается за постоянное время.

| Поле | | Описание |
|---|---|---|
| `text` | обязательно | текст тикета; ведущее `@<username CRM-бота>` отрезается |
| `chat_id` | — | Telegram ID рабочего чата, куда внешний бот отправил сообщение. Категория — по привязке чата; неизвестный чат регистрируется автоматически (название и категория — из `getChat`, либо `category`). Отключённый чат → 409 |
| `category` | — | категория, если `chat_id` не передан (по умолчанию «Внешний источник») |
| `author` | — | имя автора (по умолчанию `API`) |
| `source_message_id` | — | `message_id` сообщения внешнего бота в `chat_id`: реплаи на него попадут в тикет, а «Создан тикет #N» и ответы операторов придут реплаем на него |
| `reply_to_ticket` | — | номер тикета: добавить сообщение в него вместо создания нового (закрытый тикет переоткроется) |
| `files` | 0..n | файлы (до 20 МБ каждый, до 20 штук): загружаются документом в CRM-чат реплаем на карточку и сохраняются во вложениях тикета |
| `meta` | — | произвольный JSON (например `{"house": "Тестовая 1", "period": "10.2026", "kind": "readings", "user_id": 123}`) — сохраняется и показывается в карточке и в Mini App |

Ответ `200`: `{"ticket_number": 12, "ticket_id": 12, "created": true}` (`created: false` для `reply_to_ticket`;
при частичных проблемах — ещё `"warnings": [...]`, например если бот не смог написать в чат).
Ошибки: `401` неверный токен, `404` API выключен / тикет `reply_to_ticket` не найден, `409` чат отключён,
`413` файл больше 20 МБ, `422` неверные поля, `503` не задан `BOT_TOKEN`. Тело ошибки — `{"detail": "…"}`.

Без `source_message_id` ответы операторов уходят реплаем на сообщение «Создан тикет #N». Без `chat_id` тикет
не привязан к чату: в CRM-чат он приходит, но «Отправить клиенту» недоступно.

Пример curl:

```bash
curl -fsS https://203.0.113.10.sslip.io/api/ingest/ticket \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -F 'text=Показания за текущий 10.2026 — Тестовая 1 #Октябрь_2026 #Тестовая' \
  -F chat_id=-1001234567890 -F source_message_id=321 -F author=readings_bot \
  -F 'meta={"house": "Тестовая 1", "period": "10.2026"}' \
  -F 'files=@Тестовая_1.xlsx'
```

Пример для внешнего бота на python-telegram-bot 20.x (httpx уже идёт в зависимостях PTB). Вызывайте после
`send_document`, передавая возвращённый `Message`:

```python
import json
import logging
import os

import httpx

CRM_INGEST_URL = "https://203.0.113.10.sslip.io/api/ingest/ticket"
CRM_INGEST_TOKEN = os.environ["CRM_INGEST_TOKEN"]  # = INGEST_TOKEN в .env CRM

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def push_to_crm(sent, file_path: str, meta: dict | None = None) -> dict | None:
    """Дублирует отправленное ботом сообщение в CRM. sent — Message из send_document."""
    data = {
        "text": sent.caption or sent.text or "",
        "chat_id": str(sent.chat_id),
        "source_message_id": str(sent.message_id),
        "author": "readings_bot",
        "meta": json.dumps(meta or {}, ensure_ascii=False),
    }
    with open(file_path, "rb") as f:
        files = [("files", (os.path.basename(file_path), f.read(), XLSX))]
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(CRM_INGEST_URL, data=data, files=files,
                                  headers={"Authorization": f"Bearer {CRM_INGEST_TOKEN}"})
        r.raise_for_status()
        return r.json()  # {"ticket_number": ..., "ticket_id": ..., "created": true}
    except httpx.HTTPError as e:  # ошибка CRM не должна ломать основной бот
        logging.warning("CRM ingest failed: %s", e)
        return None


# в обработчике:
#   with open(path, "rb") as f:
#       sent = await context.bot.send_document(chat_id=-1001234567890, document=f, caption=caption)
#   await push_to_crm(sent, path, {"house": "Тестовая 1", "period": "10.2026", "kind": "readings",
#                                  "user_id": update.effective_user.id})
```

## Команды бота

| Команда | Где | Что делает |
|---|---|---|
| `@бот текст` | рабочий чат | создать тикет |
| реплай на сообщение тикета | рабочий чат | добавить сообщение в тикет |
| реплай на карточку | CRM-чат | внутренний комментарий (+ кнопка «Отправить клиенту») |
| `/t <имя>` реплаем на карточку | CRM-чат | комментарий из шаблона быстрого ответа; `/t` — список шаблонов |
| `/priority Высокий` реплаем на карточку | CRM-чат | сменить приоритет |
| `/register_chat <категория>`, `/unregister_chat` | рабочий чат | админ: регистрация чата |
| `/set_support_chat` | CRM-чат | админ: назначить CRM-чат |
| `/add_operator`, `/remove_operator` | любой | админ: операторы |
| `/id` | любой | показать ID чата и пользователя |

## Шаблоны

Подстановки в шаблонах писем и быстрых ответов: `{ticket_id}`, `{text}` (первое сообщение тикета),
`{author}`, `{category}`, `{priority}`, `{status}`, `{assignee}`. Неизвестные подстановки остаются как есть.
Метка `[#N]` добавляется в тему письма автоматически.

## Разработка и тесты

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt   # или python -m venv + pip
.venv/bin/pytest -q
```

Тесты используют SQLite в памяти и фейковый Telegram API. Прогон на PostgreSQL:
`TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/crm_test .venv/bin/pytest -q`.

## Ограничения первой версии

- Вложения во входящих письмах не сохраняются (только текст письма, цитата отрезается).
- Непрочитанные письма во «Входящих» без метки `[#N]`/связи с тикетом помечаются прочитанными и игнорируются;
  письма, отправленные с самого `GMAIL_USER`, пропускаются.
- Комментарии операторов в CRM-чате — только текстовые.
- Даты хранятся в UTC, отображаются в `TIMEZONE`.
