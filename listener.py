import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeAnimated,
)

import storage

logger = logging.getLogger(__name__)


@dataclass
class AlertMessage:
    source_title: str
    caption: str = ""
    media_bytes: Optional[bytes] = None
    media_type: str = "text"  # "text", "photo", "video", "audio", "voice", "animation", "document"
    filename: Optional[str] = None
    message_link: Optional[str] = None


AlertCallback = Callable[[AlertMessage], Awaitable[None]]

_on_alert: Optional[AlertCallback] = None


def _detect_media_type(message) -> str:
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        attrs = {type(a): a for a in doc.attributes}
        if DocumentAttributeAnimated in attrs:
            return "animation"
        if DocumentAttributeVideo in attrs:
            vid = attrs[DocumentAttributeVideo]
            return "video_note" if getattr(vid, "round_message", False) else "video"
        if DocumentAttributeAudio in attrs:
            audio = attrs[DocumentAttributeAudio]
            return "voice" if getattr(audio, "voice", False) else "audio"
        return "document"
    return "text"


def _get_filename(message) -> Optional[str]:
    media = message.media
    if not isinstance(media, MessageMediaDocument):
        return None
    from telethon.tl.types import DocumentAttributeFilename
    for attr in media.document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


async def setup(client: TelegramClient, on_alert: Optional[AlertCallback] = None) -> None:
    global _on_alert
    _on_alert = on_alert

    @client.on(events.NewMessage)
    async def handler(event):
        if not event.is_channel:
            return

        text = event.message.text or event.message.message or ""
        has_media = event.message.media is not None

        # Skip messages with no text and no media
        if not text.strip() and not has_media:
            return

        chat = await event.get_chat()
        channel_id = str(abs(chat.id))
        username = getattr(chat, "username", None)
        title = getattr(chat, "title", username or channel_id)
        key = username or channel_id
        date = event.message.date or datetime.now(timezone.utc)
        msg_id = event.message.id
        if username:
            message_link = f"https://t.me/{username}/{msg_id}"
        else:
            message_link = f"https://t.me/c/{channel_id}/{msg_id}"

        # Summary monitoring (text only)
        if storage.is_monitored(channel_id, username) and text.strip():
            storage.add_message(channel=key, text=text, date=date, sender=title)
            logger.info(f"Stored message from {title}")

        # Alert reposting
        if storage.is_alert(channel_id, username) and _on_alert:
            try:
                media_bytes = None
                media_type = "text"
                filename = None

                if has_media:
                    media_type = _detect_media_type(event.message)
                    filename = _get_filename(event.message)
                    buf = io.BytesIO()
                    await client.download_media(event.message, file=buf)
                    media_bytes = buf.getvalue()
                    logger.info(f"Downloaded {media_type} ({len(media_bytes)} bytes) from {title}")

                msg = AlertMessage(
                    source_title=title,
                    caption=text,
                    media_bytes=media_bytes,
                    media_type=media_type,
                    filename=filename,
                    message_link=message_link,
                )
                await _on_alert(msg)
            except Exception as e:
                logger.error(f"Alert repost failed for {title}: {e}")
