from datetime import datetime, timedelta, timezone

import config
import storage
from llm import get_provider

provider = get_provider()

SUMMARIZER_INSTRUCTIONS = (
    "You are a concise news summarizer. "
    "Summarize the key points clearly and briefly. "
    "Group by topic if relevant. Skip filler content."
)


def _system_prompt() -> str:
    # Append summarizer instructions to the user's SYSTEM_PROMPT so language
    # preferences (e.g. "Always respond in Ukrainian") are respected.
    return f"{config.SYSTEM_PROMPT}\n\n{SUMMARIZER_INSTRUCTIONS}"


def _format(channel: str, messages: list[dict]) -> str:
    lines = [f"=== {channel} ==="]
    for m in messages:
        date = datetime.fromisoformat(m["date"]).strftime("%b %d %H:%M")
        lines.append(f"[{date}] {m['text']}")
    return "\n".join(lines)


def summarize_channel(channel: str, hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages = storage.get_messages(channel, since=since)

    if not messages:
        return f"No messages from {channel} in the last {hours}h."

    content = _format(channel, messages)
    response = provider.chat([
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": f"Summarize:\n\n{content}"},
    ])
    return f"Summary of {channel} (last {hours}h):\n\n{response}"


def summarize_all(hours: int = 24) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    all_messages = storage.get_all_messages(since=since)

    if not all_messages:
        return f"No messages from any monitored channel in the last {hours}h."

    combined = "\n\n".join(
        _format(channel, msgs) for channel, msgs in all_messages.items()
    )
    response = provider.chat([
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": f"Summarize these Telegram channel messages:\n\n{combined}"},
    ])
    return f"Daily digest (last {hours}h):\n\n{response}"
