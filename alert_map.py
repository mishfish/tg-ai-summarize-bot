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


def save_alias(
    alias: str,
    district: str,
    city: str,
    lat: float,
    lon: float,
    db_path: str | None = None,
) -> bool:
    """Insert a location alias. Returns True if inserted, False if already exists."""
    path = _db_path(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with duckdb.connect(path) as conn:
        _ensure_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM location_aliases").fetchone()[0]
        conn.execute(
            "INSERT INTO location_aliases (alias, district, city, lat, lon)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (alias) DO NOTHING",
            [alias, district, city, lat, lon],
        )
        after = conn.execute("SELECT COUNT(*) FROM location_aliases").fetchone()[0]
    return after > before


def get_location_aliases(db_path: str | None = None) -> dict[str, dict]:
    """Return all aliases as {alias: {district, city, lat, lon}}."""
    path = _db_path(db_path)
    if not os.path.exists(path):
        return {}
    with duckdb.connect(path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT alias, district, city, lat, lon FROM location_aliases"
        ).fetchall()
    return {
        r[0]: {"district": r[1], "city": r[2], "lat": r[3], "lon": r[4]}
        for r in rows
    }


_CITY_BBOX = {
    "odesa":    (45.5, 46.75, 29.0, 31.5),   # lat_min, lat_max, lon_min, lon_max
    "mykolaiv": (46.5, 47.5,  31.5, 33.5),
}


def get_heatmap_points(
    days: int = 7,
    half_life_hours: float = 24.0,
    city_filter: str | None = None,
    db_path: str | None = None,
) -> list[tuple[float, float, float]]:
    """
    Return [(lat, lon, weight)] for alerts that mention known location aliases.
    Weight decays exponentially: exp(-age_hours / half_life_hours).
    city_filter: "odesa" or "mykolaiv" — filters aliases by bounding box.
    """
    import math

    since = datetime.now(timezone.utc) - timedelta(days=days)
    aliases = get_location_aliases(db_path)
    if not aliases:
        return []

    if city_filter and city_filter in _CITY_BBOX:
        lat_min, lat_max, lon_min, lon_max = _CITY_BBOX[city_filter]
        aliases = {
            k: v for k, v in aliases.items()
            if lat_min <= v["lat"] <= lat_max and lon_min <= v["lon"] <= lon_max
        }

    alerts = get_alerts(since=since, db_path=db_path)
    now = datetime.now(timezone.utc)

    points: list[tuple[float, float, float]] = []
    for alert in alerts:
        text_lower = alert["text"].lower()
        age_hours = (now - _ensure_utc(alert["date"])).total_seconds() / 3600
        weight = math.exp(-age_hours / half_life_hours)
        for alias, geo in aliases.items():
            if alias.lower() in text_lower:
                points.append((geo["lat"], geo["lon"], weight))
    return points


def get_alias_mentions(
    days: int = 7,
    city_filter: str | None = None,
    half_life_hours: float = 2.0,
    db_path: str | None = None,
) -> list[dict]:
    """
    Return [{alias, lat, lon, count, city, district, max_weight}] sorted by count desc.
    max_weight is the decay weight of the most recent mention (same scale as heatmap).
    """
    import math

    since = datetime.now(timezone.utc) - timedelta(days=days)
    aliases = get_location_aliases(db_path)
    if not aliases:
        return []

    if city_filter and city_filter in _CITY_BBOX:
        lat_min, lat_max, lon_min, lon_max = _CITY_BBOX[city_filter]
        aliases = {
            k: v for k, v in aliases.items()
            if lat_min <= v["lat"] <= lat_max and lon_min <= v["lon"] <= lon_max
        }

    alerts = get_alerts(since=since, db_path=db_path)
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    max_weights: dict[str, float] = {}

    for alert in alerts:
        text_lower = alert["text"].lower()
        age_hours = (now - _ensure_utc(alert["date"])).total_seconds() / 3600
        weight = math.exp(-age_hours / half_life_hours)
        for alias in aliases:
            if alias.lower() in text_lower:
                counts[alias] = counts.get(alias, 0) + 1
                if weight > max_weights.get(alias, 0):
                    max_weights[alias] = weight

    return sorted(
        [
            {
                "alias": alias,
                "lat": aliases[alias]["lat"],
                "lon": aliases[alias]["lon"],
                "city": aliases[alias]["city"],
                "district": aliases[alias]["district"],
                "count": cnt,
                "max_weight": max_weights.get(alias, 0.0),
            }
            for alias, cnt in counts.items()
        ],
        key=lambda x: -x["count"],
    )


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
