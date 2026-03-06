import logging
from datetime import time
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import storage
import summarizer
from llm import get_provider

logger = logging.getLogger(__name__)

provider = get_provider()

# Per-user LLM chat histories
histories: dict[int, list[dict]] = {}


def require_auth(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        if not storage.is_authorized(update.effective_user.id):
            await update.message.reply_text("Send the access code first.")
            return
        return await func(update, context)
    return wrapper


def get_history(user_id: int) -> list[dict]:
    if user_id not in histories:
        histories[user_id] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
    return histories[user_id]


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if storage.is_authorized(update.effective_user.id):
        await update.message.reply_text(
            "Commands:\n\n"
            "Summary channels:\n"
            "/channels — list monitored channels\n"
            "/add <channel> — start monitoring a channel\n"
            "/remove <channel> — stop monitoring\n"
            "/summary [channel] [hours] — summarize (default: all, last 24h)\n\n"
            "Alert channels (real-time repost):\n"
            "/alerts — list alert channels\n"
            "/addalert <channel> — add alert channel\n"
            "/removealert <channel> — remove alert channel\n"
            "/settarget <chat_id> — set target channel for reposts\n\n"
            "Other:\n"
            "/model — switch LLM model\n"
            "/clear — clear chat history\n"
            "/info — current settings"
        )
    else:
        await update.message.reply_text("Send the access code to get started.")


def _channel_link(channel: str) -> str:
    """Return an HTML link for a channel username, or plain text for numeric IDs."""
    if channel.lstrip("-").isdigit():
        return channel
    return f'<a href="https://t.me/{channel}">{channel}</a>'


@require_auth
async def channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ch = storage.get_channels()
    if not ch:
        await update.message.reply_text("No channels monitored. Use /add <username>")
    else:
        lines = "\n".join(f"• {_channel_link(c)}" for c in ch)
        await update.message.reply_text(
            f"Monitored channels:\n{lines}", parse_mode="HTML"
        )


@require_auth
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /add <channel_username>")
        return
    channel = context.args[0].lstrip("@")
    if storage.add_channel(channel):
        await update.message.reply_text(
            f"Now monitoring: {channel}\n\n"
            "Make sure your Telegram account (used for Telethon) is a member of this channel."
        )
    else:
        await update.message.reply_text(f"Already monitoring: {channel}")


@require_auth
async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remove <channel_username>")
        return
    channel = context.args[0].lstrip("@")
    if storage.remove_channel(channel):
        await update.message.reply_text(f"Removed: {channel}")
    else:
        await update.message.reply_text(f"Not monitoring: {channel}")


@require_auth
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    try:
        if context.args:
            channel = context.args[0].lstrip("@")
            hours = int(context.args[1]) if len(context.args) > 1 else 24
            result = summarizer.summarize_channel(channel, hours=hours)
        else:
            result = summarizer.summarize_all(hours=24)
        await update.message.reply_text(result)
    except Exception as e:
        await update.message.reply_text(f"Error generating summary: {e}")


@require_auth
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    models = provider.available_models
    keyboard = [
        [InlineKeyboardButton(
            f"{'* ' if m == provider.current_model() else ''}{m}",
            callback_data=f"model:{m}",
        )]
        for m in models
    ]
    await update.message.reply_text("Select a model:", reply_markup=InlineKeyboardMarkup(keyboard))


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not storage.is_authorized(query.from_user.id):
        await query.answer("Not authorized.")
        return
    await query.answer()
    model = query.data.split(":", 1)[1]
    provider.set_model(model)
    await query.edit_message_text(f"Switched to: {model}")


@require_auth
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    histories[update.effective_user.id] = [{"role": "system", "content": config.SYSTEM_PROMPT}]
    await update.message.reply_text("Chat history cleared.")


@require_auth
async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ch = storage.get_alert_channels()
    target = storage.get_alert_target()
    target_str = str(target) if target else "not set — use /settarget <chat_id>"
    if not ch:
        await update.message.reply_text(
            f"No alert channels.\nTarget: {target_str}\n\nUse /addalert <channel>"
        )
    else:
        lines = "\n".join(f"• {_channel_link(c)}" for c in ch)
        await update.message.reply_text(
            f"Alert channels:\n{lines}\n\nTarget: {target_str}", parse_mode="HTML"
        )


@require_auth
async def add_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /addalert <channel_username>")
        return
    channel = context.args[0].lstrip("@")
    if storage.add_alert_channel(channel):
        target = storage.get_alert_target()
        if not target:
            await update.message.reply_text(
                f"Alert channel added: {channel}\n\n"
                "No target set yet. Use /settarget <chat_id> to set where messages are reposted."
            )
        else:
            await update.message.reply_text(f"Alert channel added: {channel}")
    else:
        await update.message.reply_text(f"Already an alert channel: {channel}")


@require_auth
async def remove_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /removealert <channel_username>")
        return
    channel = context.args[0].lstrip("@")
    if storage.remove_alert_channel(channel):
        await update.message.reply_text(f"Removed alert channel: {channel}")
    else:
        await update.message.reply_text(f"Not an alert channel: {channel}")


@require_auth
async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /settarget <chat_id>\n\n"
            "The bot must be an administrator in the target channel.\n"
            "Get channel ID by forwarding a message to @userinfobot."
        )
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id must be a number (e.g. -1001234567890)")
        return
    storage.set_alert_target(chat_id)
    await update.message.reply_text(f"Alert target set to: {chat_id}")


@require_auth
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    history = get_history(user_id)
    target = storage.get_alert_target()
    await update.message.reply_text(
        f"Provider: {config.LLM_PROVIDER}\n"
        f"Model: {provider.current_model()}\n"
        f"Temperature: {config.TEMPERATURE}\n"
        f"Max tokens: {config.MAX_TOKENS}\n"
        f"Chat history: {len(history) - 1}/{config.MAX_HISTORY}\n"
        f"Summary channels: {len(storage.get_channels())}\n"
        f"Alert channels: {len(storage.get_alert_channels())}\n"
        f"Alert target: {target or 'not set'}\n"
        f"Daily digest: {config.SUMMARY_TIME} UTC"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Auth check
    if not storage.is_authorized(user_id):
        if text == config.AUTH_CODE:
            storage.authorize_user(user_id)
            await update.message.reply_text(
                "Access granted! Type /start to see available commands."
            )
        else:
            await update.message.reply_text("Wrong code. Try again.")
        return

    # LLM chat
    history = get_history(user_id)
    history.append({"role": "user", "content": text})
    if len(history) > config.MAX_HISTORY + 1:
        histories[user_id] = [history[0]] + history[-config.MAX_HISTORY:]
        history = histories[user_id]

    await update.message.chat.send_action("typing")
    try:
        reply = provider.chat(history)
        history.append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
    except Exception as e:
        history.pop()
        await update.message.reply_text(f"Error: {e}")


# --- Scheduled job ---

async def scheduled_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.SUMMARY_CHAT_ID:
        return
    try:
        result = summarizer.summarize_all(hours=24)
        await context.bot.send_message(chat_id=config.SUMMARY_CHAT_ID, text=result)
    except Exception as e:
        logger.error(f"Scheduled summary failed: {e}")


# --- App factory ---

def create_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("channels", channels))
    app.add_handler(CommandHandler("add", add_channel))
    app.add_handler(CommandHandler("remove", remove_channel))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("addalert", add_alert))
    app.add_handler(CommandHandler("removealert", remove_alert))
    app.add_handler(CommandHandler("settarget", set_target))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))

    h, m = config.SUMMARY_TIME.split(":")
    app.job_queue.run_daily(scheduled_summary, time=time(int(h), int(m)))

    return app
