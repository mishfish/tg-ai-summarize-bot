import os
from dotenv import load_dotenv

load_dotenv()

# Bot
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
AUTH_CODE = os.getenv("AUTH_CODE", "changeme")  # secret access code

# Telethon (your personal Telegram account to read channels)
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("PHONE", "")  # e.g. +1234567890

# LLM
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")  # "groq" or "anthropic"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2048"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))

# Scheduled summary — time in UTC, e.g. "09:00"
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "09:00")
# Chat ID to send scheduled summaries to (your user ID or a group ID)
SUMMARY_CHAT_ID = int(os.getenv("SUMMARY_CHAT_ID", "0"))

# Storage
MAX_MESSAGES_PER_CHANNEL = int(os.getenv("MAX_MESSAGES_PER_CHANNEL", "500"))

# Alert reposting — channel ID (e.g. -1001234567890) where the bot is admin
ALERT_TARGET_CHAT_ID = int(os.getenv("ALERT_TARGET_CHAT_ID", "0"))
