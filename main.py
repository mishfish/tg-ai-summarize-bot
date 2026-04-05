import asyncio
import io
import logging
from datetime import time as dt_time

from telegram import InputFile
from telethon import TelegramClient

import config
import storage
import listener as listener_module
from listener import AlertMessage
from bot import create_app
import legal_monitor

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main():
    if not config.TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is not set in .env")
    if not config.API_ID or not config.API_HASH:
        raise ValueError("API_ID and API_HASH are required in .env (get them from my.telegram.org)")

    storage.load()
    logger.info("Storage loaded")

    # Start bot first so we can use app.bot for the alert callback
    app = create_app()

    # Start Telethon user client
    # On first run this will ask for your phone number and OTP code
    client = TelegramClient("session/user", config.API_ID, config.API_HASH)
    await client.start(phone=config.PHONE or None)

    async def on_alert(msg: AlertMessage) -> None:
        target = storage.get_alert_target()
        if not target:
            logger.warning("Alert message received but no target chat set")
            return

        if msg.message_link:
            header = f'<b><a href="{msg.message_link}">{msg.source_title}</a></b>'
        else:
            header = f"<b>{msg.source_title}</b>"
        caption = f"{header}\n\n{msg.caption}".strip() if msg.caption else header

        if msg.media_type == "text" or msg.media_bytes is None:
            await app.bot.send_message(chat_id=target, text=caption, parse_mode="HTML")
            return

        file = InputFile(io.BytesIO(msg.media_bytes), filename=msg.filename or "file")

        if msg.media_type == "photo":
            await app.bot.send_photo(chat_id=target, photo=file, caption=caption, parse_mode="HTML")
        elif msg.media_type == "video":
            await app.bot.send_video(chat_id=target, video=file, caption=caption, parse_mode="HTML")
        elif msg.media_type == "video_note":
            await app.bot.send_video_note(chat_id=target, video_note=file)
        elif msg.media_type == "audio":
            await app.bot.send_audio(chat_id=target, audio=file, caption=caption, parse_mode="HTML")
        elif msg.media_type == "voice":
            await app.bot.send_voice(chat_id=target, voice=file, caption=caption, parse_mode="HTML")
        elif msg.media_type == "animation":
            await app.bot.send_animation(chat_id=target, animation=file, caption=caption, parse_mode="HTML")
        else:  # document / unknown
            await app.bot.send_document(chat_id=target, document=file, caption=caption, parse_mode="HTML")

    await listener_module.setup(client, on_alert=on_alert)
    logger.info("Telethon listener started")

    async def scheduled_legal_monitor(context) -> None:
        try:
            stats = await legal_monitor.run(max_pages=config.LEGAL_MONITOR_PAGES)
            logger.info(
                "Legal monitor: %d new bills, %d total", stats["new"], stats["total"]
            )
        except Exception as exc:
            logger.error("Scheduled legal monitor failed: %s", exc)

    async with app:
        await app.start()
        await app.updater.start_polling()
        logger.info(f"Bot started | provider={config.LLM_PROVIDER} | model={config.GROQ_MODEL if config.LLM_PROVIDER == 'groq' else config.ANTHROPIC_MODEL}")

        lh, lm = config.LEGAL_MONITOR_TIME.split(":")
        app.job_queue.run_daily(
            scheduled_legal_monitor,
            time=dt_time(int(lh), int(lm)),
            job_kwargs={"misfire_grace_time": 7200},
        )
        logger.info("Legal monitor scheduled at %s UTC", config.LEGAL_MONITOR_TIME)

        # Run until Ctrl+C
        try:
            await client.run_until_disconnected()
        except KeyboardInterrupt:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
