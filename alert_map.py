import logging
import os
from datetime import datetime, timezone, timedelta

import duckdb

import config

logger = logging.getLogger(__name__)


def _db_path(db_path: str | None) -> str:
    return db_path if db_path is not None else config.DUCKDB_PATH


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            msg_id  BIGINT,
            channel TEXT,
            text    TEXT NOT NULL,
            date    TIMESTAMP NOT NULL,
            PRIMARY KEY (msg_id, channel)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS location_aliases (
            alias    TEXT PRIMARY KEY,
            district TEXT NOT NULL,
            city     TEXT NOT NULL,
            lat      REAL NOT NULL,
            lon      REAL NOT NULL
        )
    """)


def save_alert(
    msg_id: int,
    channel: str,
    text: str,
    date: datetime,
    db_path: str | None = None,
) -> bool:
    """Persist one alert message. Returns True if new, False if duplicate."""
    path = _db_path(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with duckdb.connect(path) as conn:
        _ensure_schema(conn)
        conn.begin()
        try:
            before = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            conn.execute(
                "INSERT INTO alerts (msg_id, channel, text, date)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (msg_id, channel) DO NOTHING",
                [msg_id, channel, text, _ensure_utc(date)],
            )
            after = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return after > before


def get_alerts(
    since: datetime | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """Return alerts ordered newest-first, optionally filtered by date."""
    path = _db_path(db_path)
    if not os.path.exists(path):
        return []
    with duckdb.connect(path) as conn:
        _ensure_schema(conn)
        if since:
            rows = conn.execute(
                "SELECT msg_id, channel, text, date FROM alerts"
                " WHERE date >= ? ORDER BY date DESC",
                [_ensure_utc(since)],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT msg_id, channel, text, date FROM alerts ORDER BY date DESC"
            ).fetchall()
    return [
        {"msg_id": r[0], "channel": r[1], "text": r[2], "date": r[3]}
        for r in rows
    ]


async def fetch_history(
    client,
    channels: list[str],
    days: int = 60,
    db_path: str | None = None,
) -> dict:
    """
    Fetch message history from Telethon channels and persist to DB.
    Returns {"fetched": N, "new": M}.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    fetched = 0
    new_count = 0

    for channel in channels:
        try:
            async for msg in client.iter_messages(channel):
                msg_date = _ensure_utc(msg.date)
                if msg_date < since:
                    break
                if not msg.text or not msg.text.strip():
                    continue
                fetched += 1
                is_new = save_alert(msg.id, channel, msg.text, msg_date, db_path=db_path)
                if is_new:
                    new_count += 1
        except Exception as e:
            logger.warning("fetch_history: skipping channel %s: %s", channel, e)

    logger.info(
        "fetch_history: %d fetched, %d new across %d channels",
        fetched, new_count, len(channels),
    )
    return {"fetched": fetched, "new": new_count}
