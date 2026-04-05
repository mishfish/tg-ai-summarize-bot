import json
import logging
import os
import re
from datetime import datetime, timezone

import duckdb
from playwright.async_api import async_playwright

import config

logger = logging.getLogger(__name__)

_BILLS_URL = "https://itd.rada.gov.ua/billinfo/Bills/period"
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


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
    json_path: str | None = None,
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
    if json_path is None:
        json_path = os.path.join(_DATA_DIR, "bills.json")
    if db_path is None:
        db_path = config.DUCKDB_PATH

    os.makedirs(os.path.dirname(json_path), exist_ok=True)
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


async def scrape_bills(max_pages: int = 0) -> list[dict]:
    """
    Scrape all bills from itd.rada.gov.ua using headless Chromium.

    Args:
        max_pages: maximum pages to scrape (0 = auto-detect and scrape all)

    Returns:
        list of {"title", "link", "page"} dicts
    """
    all_bills: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        # Step 1: detect total pages
        detected_max = max_pages
        if detected_max == 0:
            try:
                await page.goto(_BILLS_URL, wait_until="networkidle", timeout=30_000)
                await page.wait_for_timeout(3_000)
                result = await page.evaluate("""
                    (function() {
                        const buttons = document.querySelectorAll('button.page-link, a.page-link');
                        if (buttons.length === 0) return "1";
                        let maxVal = 1;
                        buttons.forEach(btn => {
                            const val = parseInt(btn.value || btn.innerText || btn.textContent);
                            if (!isNaN(val) && val > maxVal) maxVal = val;
                        });
                        return maxVal.toString();
                    })()
                """)
                detected_max = int(result)
                logger.info("Detected %d pages", detected_max)
            except Exception as exc:
                logger.warning("Page detection failed, defaulting to 50: %s", exc)
                detected_max = 50

        # Step 2: scrape each page
        for page_num in range(1, detected_max + 1):
            url = f"{_BILLS_URL}?page={page_num}"
            logger.info("Scraping page %d/%d", page_num, detected_max)

            page_bills: list[dict] = []
            last_err = None

            for attempt in range(2):
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    await page.wait_for_timeout(3_000)
                    rows = await page.evaluate("""
                        Array.from(document.querySelectorAll('tbody tr')).map(tr => {
                            const a = tr.querySelector('td:nth-child(2) a');
                            const td = tr.querySelector('td:nth-child(4)');
                            return {
                                link: a ? a.href : '',
                                title: td ? td.innerText.trim() : ''
                            };
                        })
                    """)
                    page_bills = [
                        {"title": r["title"], "link": r["link"], "page": page_num}
                        for r in rows
                        if r.get("title")
                    ]
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
                    logger.warning("Page %d attempt %d failed: %s", page_num, attempt + 1, exc)

            if last_err:
                logger.error("Skipping page %d after retries: %s", page_num, last_err)
                continue

            if not page_bills:
                logger.info("No bills on page %d, stopping", page_num)
                break

            all_bills.extend(page_bills)
            logger.info("Page %d: %d bills (total %d)", page_num, len(page_bills), len(all_bills))

            await page.wait_for_timeout(300)

        await browser.close()

    return all_bills


async def run(
    max_pages: int = 0,
    json_path: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Scrape bills and persist them. Returns {"new": N, "total": M}."""
    bills = await scrape_bills(max_pages=max_pages)
    return save_bills(bills, json_path=json_path, db_path=db_path)
