import json
import logging
import re
from datetime import datetime, timezone

import duckdb
from playwright.async_api import async_playwright

import config

logger = logging.getLogger(__name__)

_BILLS_URL = "https://itd.rada.gov.ua/billinfo/Bills/period"


def _extract_id(link: str) -> int | None:
    """Extract numeric bill ID from a Card URL, e.g. .../Bills/Card/69567 → 69567."""
    match = re.search(r"/Bills/Card/(\d+)", link)
    if not match:
        return None
    return int(match.group(1))


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id         INTEGER PRIMARY KEY,
            title      TEXT NOT NULL,
            link       TEXT NOT NULL,
            scraped_at TIMESTAMP NOT NULL
        )
    """)


def save_bills(
    bills: list[dict],
    json_path: str = "data/bills.json",
    db_path: str | None = None,
) -> dict:
    """
    Persist bills to JSON and DuckDB.

    Args:
        bills: list of {"title", "link", "page"} dicts from scrape_bills()
        json_path: path to write/overwrite bills JSON
        db_path: DuckDB file path; defaults to config.DUCKDB_PATH

    Returns:
        {"total": <count in db>, "new": <inserted this call>}
    """
    if db_path is None:
        db_path = config.DUCKDB_PATH

    now = datetime.now(timezone.utc)

    with duckdb.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.begin()
        try:
            before = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]

            for bill in bills:
                bill_id = _extract_id(bill.get("link", ""))
                if bill_id is None:
                    logger.warning("Skipping bill with no valid ID: %s", bill.get("link"))
                    continue

                title = bill.get("title", "")
                link = bill.get("link", "")
                if not title:
                    logger.warning("Skipping bill with no title")
                    continue

                conn.execute(
                    "INSERT INTO bills (id, title, link, scraped_at) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                    [bill_id, title, link, now],
                )

            after = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
            new_count = after - before

            # Load all bills from DB for JSON export
            all_rows = conn.execute(
                "SELECT id, title, link, scraped_at FROM bills ORDER BY id ASC"
            ).fetchall()

            all_bills_for_json = [
                {
                    "id": row[0],
                    "title": row[1],
                    "link": row[2],
                    "scraped_at": str(row[3]),
                }
                for row in all_rows
            ]

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(all_bills_for_json, f, ensure_ascii=False, indent=2)

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    total = len(all_bills_for_json)
    logger.info("save_bills: %d new, %d total", new_count, total)
    return {"new": new_count, "total": total}
