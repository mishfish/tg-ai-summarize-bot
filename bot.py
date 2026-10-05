import logging
from datetime import time
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
import legal_monitor
import alert_map
import map_render

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
            "/info — current settings\n"
            "/bills [pages] — scrape Verkhovna Rada bills\n"
            "/fetchalerts [days] — завантажити історію алертів (default: 60 днів)\n"
            "/map [days] [odesa|mykolaiv] — теплова карта алертів\n"
            "/addalias назва | район | місто | lat | lon — додати локацію\n"
            "/startlive [хв] [odesa|mykolaiv] — live-карта (default: 10 хв)\n"
            "/stoplive — зупинити оновлення"
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
        limit = 4096
        for i in range(0, len(result), limit):
            await update.message.reply_text(result[i:i + limit])
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


@require_auth
async def bills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action("typing")
    try:
        max_pages = int(context.args[0]) if context.args else config.LEGAL_MONITOR_PAGES
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /bills [pages] — pages is optional integer")
        return
    try:
        stats = await legal_monitor.run(max_pages=max_pages)
        await update.message.reply_text(
            f"Знайдено на сайті: {stats['scraped']} законопроєктів.\n"
            f"Збережено нових: {stats['new']}.\n"
            f"Всього в базі: {stats['total']}."
        )
    except Exception as e:
        logger.error("bills_command failed: %s", e)
        await update.message.reply_text(f"Помилка: {e}")


@require_auth
async def fetchalerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        days = int(context.args[0]) if context.args else 60
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /fetchalerts [days]")
        return

    client = context.bot_data.get("telethon_client")
    if client is None:
        await update.message.reply_text("Telethon client недоступний.")
        return

    channels = storage.get_alert_channels()
    if not channels:
        await update.message.reply_text("Немає налаштованих алерт-каналів.")
        return

    await update.message.reply_text(
        f"Завантажую {days} дн. з {len(channels)} каналів… це може зайняти кілька хвилин."
    )
    try:
        stats = await alert_map.fetch_history(client, channels, days=days)
        await update.message.reply_text(
            f"Готово. Перевірено: {stats['fetched']} повідомлень, "
            f"збережено нових: {stats['new']}."
        )
    except Exception as e:
        logger.error("fetchalerts_command failed: %s", e)
        await update.message.reply_text(f"Помилка: {e}")


_CITY_ALIASES = {"odesa": "odesa", "одеса": "odesa", "mykolaiv": "mykolaiv", "миколаїв": "mykolaiv"}

_ADDALIAS_USAGE = (
    "Usage: /addalias назва | район | місто | lat | lon\n"
    "Приклад: /addalias Котовського | Суворовський | Одеса | 46.4069 | 30.6667"
)


@require_auth
async def addalias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or [])
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 5:
        await update.message.reply_text(_ADDALIAS_USAGE)
        return
    alias, district, city, lat_s, lon_s = parts
    if not alias:
        await update.message.reply_text(_ADDALIAS_USAGE)
        return
    try:
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        await update.message.reply_text("lat і lon мають бути числами.\n" + _ADDALIAS_USAGE)
        return
    is_new = alert_map.save_alias(alias, district, city, lat, lon)
    if is_new:
        await update.message.reply_text(f"Збережено: {alias} ({district}, {city}) → {lat}, {lon}")
    else:
        await update.message.reply_text(f"Вже існує: {alias}. Щоб оновити — видали спочатку через термінал.")


@require_auth
async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = 7
    city = None
    for arg in (context.args or []):
        if arg.lower() in _CITY_ALIASES:
            city = _CITY_ALIASES[arg.lower()]
        else:
            try:
                days = int(arg)
            except ValueError:
                await update.message.reply_text(
                    "Usage: /map [days] [odesa|mykolaiv]\nПриклади: /map 14  /map 7 odesa  /map mykolaiv"
                )
                return

    await update.message.chat.send_action("upload_photo")
    try:
        points = alert_map.get_heatmap_points(days=days, city_filter=city, half_life_hours=24)
        mentions = alert_map.get_alias_mentions(days=days, city_filter=city, half_life_hours=24)
        if not points:
            aliases = alert_map.get_location_aliases()
            if not aliases:
                await update.message.reply_text(
                    "location_aliases порожня. Спочатку запусти:\n"
                    "<code>python extract_places.py</code>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"За останні {days} дн. не знайдено повідомлень з відомими локаціями."
                )
            return

        png = await map_render.render_heatmap(points, days=days, city=city, mentions=mentions)
        await update.message.reply_photo(photo=png)
    except Exception as e:
        logger.error("map_command failed: %s", e)
        await update.message.reply_text(f"Помилка генерації карти: {e}")


async def _live_map_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recurring job: regenerate map and edit the pinned message."""
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    city = data.get("city")
    days = data.get("days", 1)

    try:
        half_life = data.get("half_life", 2.0)
        points = alert_map.get_heatmap_points(days=days, city_filter=city, half_life_hours=half_life)
        mentions = alert_map.get_alias_mentions(days=days, city_filter=city, half_life_hours=half_life)
        png = await map_render.render_heatmap(points, days=days, city=city, mentions=mentions)
        await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaPhoto(media=png),
        )
    except Exception as e:
        logger.error("live_map_job failed: %s", e)


async def startlive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg_obj = update.effective_message
    if not chat or chat.type == "private":
        if not update.effective_user or not storage.is_authorized(update.effective_user.id):
            await msg_obj.reply_text("Send the access code first.")
            return
    interval = 10
    city = None
    days = 1
    half_life = 2.0
    min_interval = 1
    for arg in (context.args or []):
        a = arg.lower()
        if a in _CITY_ALIASES:
            city = _CITY_ALIASES[a]
        elif a.endswith("h") and a[:-1].replace(".", "").isdigit():
            half_life = float(a[:-1])
        elif a.endswith("m") and a[:-1].replace(".", "").isdigit():
            half_life = float(a[:-1]) / 60
        else:
            try:
                interval = max(min_interval, int(arg))
            except ValueError:
                pass

    chat_id = chat.id
    for job in context.job_queue.get_jobs_by_name(f"live_{chat_id}"):
        job.schedule_removal()

    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    points = alert_map.get_heatmap_points(days=days, city_filter=city, half_life_hours=half_life)
    mentions = alert_map.get_alias_mentions(days=days, city_filter=city, half_life_hours=half_life)
    png = await map_render.render_heatmap(points, days=days, city=city, mentions=mentions)
    decay_str = f"{half_life:.4g}h" if half_life >= 1 else f"{half_life*60:.4g}m"
    sent = await context.bot.send_photo(
        chat_id=chat_id,
        photo=png,
        caption=f"Live-карта · оновлення кожні {interval} хв. · decay {decay_str}",
    )

    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
        pinned = True
    except Exception:
        pinned = False

    context.job_queue.run_repeating(
        _live_map_job,
        interval=interval * 60,
        first=interval * 60,
        name=f"live_{chat_id}",
        data={"chat_id": chat_id, "message_id": sent.message_id, "city": city, "days": days, "half_life": half_life},
    )
    note = "" if pinned else " (дай боту права адміна щоб закріплювати автоматично)"
    await context.bot.send_message(chat_id=chat_id, text=f"Запущено. Оновлення кожні {interval} хв., decay {decay_str}.{note}")


async def stoplive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type == "private":
        if not update.effective_user or not storage.is_authorized(update.effective_user.id):
            await update.effective_message.reply_text("Send the access code first.")
            return
    chat_id = chat.id
    jobs = context.job_queue.get_jobs_by_name(f"live_{chat_id}")
    if jobs:
        for job in jobs:
            job.schedule_removal()
        await context.bot.send_message(chat_id=chat_id, text="Live-карту зупинено.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Live-карта не запущена.")


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
        limit = 4096
        for i in range(0, len(result), limit):
            await context.bot.send_message(chat_id=config.SUMMARY_CHAT_ID, text=result[i:i + limit])
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
    app.add_handler(CommandHandler("bills", bills_command))
    app.add_handler(CommandHandler("fetchalerts", fetchalerts_command))
    app.add_handler(CommandHandler("map", map_command))
    app.add_handler(CommandHandler("addalias", addalias_command))
    app.add_handler(CommandHandler("startlive", startlive_command))
    app.add_handler(CommandHandler("stoplive", stoplive_command))
    app.add_handler(CallbackQueryHandler(model_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))

    h, m = config.SUMMARY_TIME.split(":")
    app.job_queue.run_daily(
            scheduled_summary,
            time=time(int(h), int(m)),
            job_kwargs={"misfire_grace_time": 7200}
            )

    return app
