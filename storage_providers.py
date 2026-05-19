"""
Storage provider abstraction.

Providers are configured via STORAGE_PROVIDERS env var (comma-separated):
  STORAGE_PROVIDERS=json          → only JSON files (default)
  STORAGE_PROVIDERS=duckdb        → only DuckDB
  STORAGE_PROVIDERS=json,duckdb   → both simultaneously (writes fan out; reads from json)
"""

import json
import os
from abc import ABC, abstractmethod

try:
    import duckdb as _duckdb
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False


class StorageProvider(ABC):
    """Persist and retrieve messages + bot state."""

    @abstractmethod
    def load_messages(self) -> dict:
        """Return {channel: [{"text", "date", "sender"}, ...]}."""

    @abstractmethod
    def load_state(self) -> dict:
        """Return {monitored_channels, alert_channels, alert_target, authorized_users}."""

    @abstractmethod
    def save_messages(self, messages: dict) -> None: ...

    @abstractmethod
    def save_state(self, state: dict) -> None: ...


# ---------------------------------------------------------------------------
# JSON file provider (original implementation)
# ---------------------------------------------------------------------------

class JsonFileProvider(StorageProvider):
    def __init__(self, data_dir: str = "data"):
        os.makedirs(data_dir, exist_ok=True)
        self._messages_file = os.path.join(data_dir, "messages.json")
        self._state_file = os.path.join(data_dir, "state.json")

    def load_messages(self) -> dict:
        if os.path.exists(self._messages_file):
            with open(self._messages_file) as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
        return {}

    def load_state(self) -> dict:
        if os.path.exists(self._state_file):
            with open(self._state_file) as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
        return {}

    def save_messages(self, messages: dict) -> None:
        with open(self._messages_file, "w") as f:
            json.dump(messages, f)

    def save_state(self, state: dict) -> None:
        with open(self._state_file, "w") as f:
            json.dump(state, f)


# ---------------------------------------------------------------------------
# DuckDB provider
# ---------------------------------------------------------------------------

class DuckDBProvider(StorageProvider):
    def __init__(self, db_path: str = "data/storage.db"):
        if not _DUCKDB_AVAILABLE:
            raise ImportError(
                "duckdb is not installed. Run: pip install duckdb"
            )
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._conn = _duckdb.connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                channel TEXT NOT NULL,
                text    TEXT NOT NULL,
                date    TEXT NOT NULL,
                sender  TEXT NOT NULL DEFAULT ''
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS monitored_channels (
                channel TEXT PRIMARY KEY
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_channels (
                channel TEXT PRIMARY KEY
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id BIGINT PRIMARY KEY
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

    def load_messages(self) -> dict:
        rows = self._conn.execute(
            "SELECT channel, text, date, sender FROM messages ORDER BY date"
        ).fetchall()
        result: dict = {}
        for channel, text, date, sender in rows:
            result.setdefault(channel, []).append(
                {"text": text, "date": date, "sender": sender}
            )
        return result

    def load_state(self) -> dict:
        monitored = [
            r[0] for r in
            self._conn.execute("SELECT channel FROM monitored_channels").fetchall()
        ]
        alerts = [
            r[0] for r in
            self._conn.execute("SELECT channel FROM alert_channels").fetchall()
        ]
        users = [
            r[0] for r in
            self._conn.execute("SELECT user_id FROM authorized_users").fetchall()
        ]
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'alert_target'"
        ).fetchone()
        alert_target = int(row[0]) if row else 0
        return {
            "monitored_channels": monitored,
            "alert_channels": alerts,
            "authorized_users": users,
            "alert_target": alert_target,
        }

    def save_messages(self, messages: dict) -> None:
        rows = [
            (channel, msg["text"], msg["date"], msg.get("sender", ""))
            for channel, msgs in messages.items()
            for msg in msgs
        ]
        self._conn.execute("DELETE FROM messages")
        if rows:
            self._conn.executemany(
                "INSERT INTO messages (channel, text, date, sender) VALUES (?, ?, ?, ?)",
                rows,
            )

    def save_state(self, state: dict) -> None:
        self._conn.execute("DELETE FROM monitored_channels")
        self._conn.execute("DELETE FROM alert_channels")
        self._conn.execute("DELETE FROM authorized_users")

        for ch in state.get("monitored_channels", []):
            self._conn.execute(
                "INSERT INTO monitored_channels VALUES (?) ON CONFLICT DO NOTHING", [ch]
            )
        for ch in state.get("alert_channels", []):
            self._conn.execute(
                "INSERT INTO alert_channels VALUES (?) ON CONFLICT DO NOTHING", [ch]
            )
        for uid in state.get("authorized_users", []):
            self._conn.execute(
                "INSERT INTO authorized_users VALUES (?) ON CONFLICT DO NOTHING", [uid]
            )
        self._conn.execute(
            "INSERT INTO settings VALUES ('alert_target', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [str(state.get("alert_target", 0))],
        )


# ---------------------------------------------------------------------------
# Composite provider — writes fan out to all; reads from primary (first)
# ---------------------------------------------------------------------------

class CompositeProvider(StorageProvider):
    def __init__(self, providers: list[StorageProvider]):
        if not providers:
            raise ValueError("CompositeProvider requires at least one provider")
        self._providers = providers

    def load_messages(self) -> dict:
        return self._providers[0].load_messages()

    def load_state(self) -> dict:
        return self._providers[0].load_state()

    def save_messages(self, messages: dict) -> None:
        for p in self._providers:
            p.save_messages(messages)

    def save_state(self, state: dict) -> None:
        for p in self._providers:
            p.save_state(state)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_provider(
    provider_names: list[str],
    data_dir: str = "data",
    duckdb_path: str = "data/storage.db",
) -> StorageProvider:
    """
    Build a provider from a list of names.

    Examples:
      create_provider(["json"])           → JsonFileProvider
      create_provider(["duckdb"])         → DuckDBProvider
      create_provider(["json", "duckdb"]) → CompositeProvider([Json, DuckDB])
    """
    _factories = {
        "json":   lambda: JsonFileProvider(data_dir),
        "duckdb": lambda: DuckDBProvider(duckdb_path),
    }
    providers: list[StorageProvider] = []
    for name in provider_names:
        name = name.strip().lower()
        if name not in _factories:
            raise ValueError(
                f"Unknown storage provider: {name!r}. Valid options: {list(_factories)}"
            )
        providers.append(_factories[name]())

    return providers[0] if len(providers) == 1 else CompositeProvider(providers)
