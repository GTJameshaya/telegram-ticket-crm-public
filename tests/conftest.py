import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["ADMIN_TG_IDS"] = "1001"
os.environ["GMAIL_USER"] = ""
for _k in ("GMAIL_APP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
    os.environ[_k] = ""

import itertools  # noqa: E402

import pytest  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.methods import CopyMessage, GetChat, SendDocument, SendMessage  # noqa: E402
from aiogram.types import Chat as TgChat, ChatFullInfo, Message as TgMessage, MessageId, User as TgUser  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool, StaticPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import set_sessionmaker  # noqa: E402
from app.models import Base  # noqa: E402

get_settings.cache_clear()

BOT_ID = 999
BOT_USERNAME = "crm_test_bot"


# По умолчанию тесты идут на SQLite в памяти. Чтобы прогнать на PostgreSQL:
#   TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/crm_test pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def sessionmaker():
    if TEST_DATABASE_URL:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                     connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(sm)
    yield sm
    await engine.dispose()


@pytest.fixture
async def session(sessionmaker):
    async with sessionmaker() as s:
        yield s


class FakeBot(Bot):
    """Бот без сети: запоминает вызовы API и возвращает правдоподобные ответы."""

    def __init__(self):
        super().__init__("123456:TEST-TOKEN")
        self.calls: list = []
        self._ids = itertools.count(5000)
        self.chat_titles: dict[int, str] = {}

    async def me(self):
        return TgUser(id=BOT_ID, is_bot=True, first_name="CRM", username=BOT_USERNAME)

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        result = True
        if isinstance(method, SendMessage):
            result = TgMessage(message_id=next(self._ids), date=0, text=method.text,
                               chat=TgChat(id=method.chat_id, type="supergroup"))
        elif isinstance(method, CopyMessage):
            result = MessageId(message_id=next(self._ids))
        elif isinstance(method, SendDocument):
            n = next(self._ids)
            name = getattr(method.document, "filename", None) or "file"
            result = TgMessage(message_id=n, date=0, chat=TgChat(id=method.chat_id, type="supergroup"),
                               document={"file_id": f"FILE{n}", "file_unique_id": f"U{n}", "file_name": name,
                                         "mime_type": "application/octet-stream"})
        elif isinstance(method, GetChat):
            result = ChatFullInfo(id=method.chat_id, type="group", title=self.chat_titles.get(method.chat_id, ""),
                                  accent_color_id=0, max_reaction_count=0,
                                  accepted_gift_types={"unlimited_gifts": False, "limited_gifts": False,
                                                       "unique_gifts": False, "premium_subscription": False,
                                                       "gifts_from_channels": False})
        method._fake_result = result
        return result

    def of_type(self, cls, chat_id=None):
        return [c for c in self.calls if isinstance(c, cls) and (chat_id is None or c.chat_id == chat_id)]

    def sent(self, chat_id=None):
        return [c for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]


@pytest.fixture
def bot():
    return FakeBot()
