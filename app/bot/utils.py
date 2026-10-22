"""Вспомогательные функции для сообщений Telegram."""
import re

from aiogram.types import Message, User as TgUser

from app.services.tickets import AttachmentData


def display_name(user: TgUser | None) -> str:
    if user is None:
        return ""
    name = user.full_name
    return f"{name} (@{user.username})" if user.username else name


def message_text(message: Message) -> str:
    return message.text or message.caption or ""


def mentions_bot(message: Message, bot_username: str, bot_id: int) -> bool:
    """Есть ли в тексте/подписи упоминание бота (@username или text_mention)."""
    text = message_text(message)
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "mention" and ent.extract_from(text).lower() == f"@{bot_username}".lower():
            return True
        if ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
            return True
    return False


def strip_mention(text: str, bot_username: str) -> str:
    cleaned = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def extract_attachments(message: Message) -> list[AttachmentData]:
    """Фото, документы, видео, голосовые и т.п. из сообщения."""
    out: list[AttachmentData] = []
    if message.photo:
        p = message.photo[-1]  # самое большое разрешение
        out.append(AttachmentData(tg_file_id=p.file_id, file_type="photo", file_name="photo.jpg", mime="image/jpeg"))
    for ftype in ("animation", "document", "video", "voice", "audio", "video_note"):
        obj = getattr(message, ftype, None)
        if obj is None or any(a.tg_file_id == obj.file_id for a in out):
            continue
        out.append(AttachmentData(
            tg_file_id=obj.file_id,
            file_type=ftype,
            file_name=getattr(obj, "file_name", None) or ftype,
            mime=getattr(obj, "mime_type", None) or "",
        ))
    return out
