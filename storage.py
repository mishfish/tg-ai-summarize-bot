import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import config

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs("session", exist_ok=True)

MESSAGES_FILE = os.path.join(DATA_DIR, "messages.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# channel_name -> deque of {"text", "date", "sender"}
_messages: dict[str, deque] = {}
_monitored_channels: set[str] = set()
_alert_channels: set[str] = set()
_alert_target: int = 0  # chat ID where bot reposts alert messages
_authorized_users: set[int] = set()


def load() -> None:
    global _messages, _monitored_channels, _alert_channels, _alert_target, _authorized_users
    if os.path.exists(MESSAGES_FILE):
        with open(MESSAGES_FILE) as f:
            data = json.load(f)
        _messages = {
            k: deque(v, maxlen=config.MAX_MESSAGES_PER_CHANNEL)
            for k, v in data.items()
        }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        _monitored_channels = set(state.get("monitored_channels", []))
        _alert_channels = set(state.get("alert_channels", []))
        _alert_target = state.get("alert_target", config.ALERT_TARGET_CHAT_ID)
        _authorized_users = set(state.get("authorized_users", []))


def _save() -> None:
    with open(MESSAGES_FILE, "w") as f:
        json.dump({k: list(v) for k, v in _messages.items()}, f)
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "monitored_channels": list(_monitored_channels),
                "alert_channels": list(_alert_channels),
                "alert_target": _alert_target,
                "authorized_users": list(_authorized_users),
            },
            f,
        )


# --- Messages ---

def add_message(channel: str, text: str, date: datetime, sender: str = "") -> None:
    if channel not in _messages:
        _messages[channel] = deque(maxlen=config.MAX_MESSAGES_PER_CHANNEL)
    _messages[channel].append(
        {"text": text, "date": date.isoformat(), "sender": sender}
    )
    _save()


def get_messages(channel: str, since: Optional[datetime] = None) -> list[dict]:
    msgs = list(_messages.get(channel, []))
    if since:
        msgs = [m for m in msgs if datetime.fromisoformat(m["date"]) >= since]
    return msgs


def get_all_messages(since: Optional[datetime] = None) -> dict[str, list[dict]]:
    result = {}
    for channel in _monitored_channels:
        msgs = get_messages(channel, since)
        if msgs:
            result[channel] = msgs
    return result


# --- Channels ---

def add_channel(channel: str) -> bool:
    if channel in _monitored_channels:
        return False
    _monitored_channels.add(channel)
    _save()
    return True


def remove_channel(channel: str) -> bool:
    if channel not in _monitored_channels:
        return False
    _monitored_channels.discard(channel)
    _save()
    return True


def get_channels() -> list[str]:
    return sorted(_monitored_channels)


def is_monitored(channel_id: str, username: Optional[str]) -> bool:
    return channel_id in _monitored_channels or (
        username is not None and username in _monitored_channels
    )


# --- Alert channels ---

def add_alert_channel(channel: str) -> bool:
    if channel in _alert_channels:
        return False
    _alert_channels.add(channel)
    _save()
    return True


def remove_alert_channel(channel: str) -> bool:
    if channel not in _alert_channels:
        return False
    _alert_channels.discard(channel)
    _save()
    return True


def get_alert_channels() -> list[str]:
    return sorted(_alert_channels)


def is_alert(channel_id: str, username: Optional[str]) -> bool:
    return channel_id in _alert_channels or (
        username is not None and username in _alert_channels
    )


def set_alert_target(chat_id: int) -> None:
    global _alert_target
    _alert_target = chat_id
    _save()


def get_alert_target() -> int:
    return _alert_target


# --- Auth ---

def authorize_user(user_id: int) -> None:
    _authorized_users.add(user_id)
    _save()


def is_authorized(user_id: int) -> bool:
    return user_id in _authorized_users
