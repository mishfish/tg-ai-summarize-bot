# Legal Monitor Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Python module that scrapes Ukrainian parliament bills from itd.rada.gov.ua and persists them to JSON and DuckDB, with a `/bills` bot command and a daily scheduled job.

**Architecture:** Single `legal_monitor.py` module with async `scrape_bills` (Playwright/Chromium) and sync `save_bills` (JSON + DuckDB). Integrated into `bot.py` (command) and `main.py` (scheduled job). Config values added to `config.py`.

**Tech Stack:** playwright (async), duckdb, python-telegram-bot job-queue, pytest, unittest.mock

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `legal_monitor.py` | Create | Scraping + storage logic |
| `tests/test_legal_monitor.py` | Create | Unit tests for save/parse logic |
| `requirements.txt` | Modify | Add `playwright` |
| `config.py` | Modify | Add `LEGAL_MONITOR_TIME`, `LEGAL_MONITOR_PAGES` |
| `bot.py` | Modify | Add `/bills` command handler |
| `main.py` | Modify | Register scheduled job |

---

### Task 1: Config and dependencies

**Files:**
- Modify: `requirements.txt`
- Modify: `config.py`

- [ ] **Step 1: Add playwright to requirements.txt**

Open `requirements.txt` and add one line at the end:

```
playwright
```

Full file after edit:
```
python-telegram-bot[job-queue]>=21.0
telethon>=1.36.0
groq>=0.9.0
anthropic>=0.40.0
python-dotenv>=1.0.0
duckdb>=1.0.0          # optional: enable with STORAGE_PROVIDERS=duckdb or json,duckdb
playwright
```

- [ ] **Step 2: Add config vars to config.py**

At the end of `config.py`, append:

```python
# Legal monitor
LEGAL_MONITOR_TIME = os.getenv("LEGAL_MONITOR_TIME", "07:00")  # UTC HH:MM
LEGAL_MONITOR_PAGES = int(os.getenv("LEGAL_MONITOR_PAGES", "0"))  # 0 = all
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt config.py
git commit -m "chore: add playwright dep and legal monitor config vars"
```

---

### Task 2: Core module — save_bills and helpers

**Files:**
- Create: `legal_monitor.py`
- Create: `tests/test_legal_monitor.py`

- [ ] **Step 1: Write failing tests for `_extract_id` and `save_bills`**

Create `tests/test_legal_monitor.py`:

```python
import json
import os
import tempfile
from datetime import datetime, timezone

import duckdb
import pytest

import legal_monitor


def test_extract_id_from_card_link():
    link = "https://itd.rada.gov.ua/billinfo/Bills/Card/69567"
    assert legal_monitor._extract_id(link) == 69567


def test_extract_id_returns_none_for_invalid_link():
    assert legal_monitor._extract_id("https://example.com/foo") is None
    assert legal_monitor._extract_id("") is None


def test_save_bills_writes_json_and_returns_stats(tmp_path):
    json_path = str(tmp_path / "bills.json")
    db_path = str(tmp_path / "test.db")

    bills = [
        {
            "title": "Проект Закону про тест",
            "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/100",
            "page": 1,
        },
        {
            "title": "Проект Закону про тест 2",
            "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/101",
            "page": 1,
        },
    ]

    stats = legal_monitor.save_bills(bills, json_path=json_path, db_path=db_path)

    assert stats["new"] == 2
    assert stats["total"] == 2

    with open(json_path) as f:
        saved = json.load(f)
    assert len(saved) == 2
    assert saved[0]["id"] == 100
    assert saved[0]["title"] == "Проект Закону про тест"
    assert "scraped_at" in saved[0]


def test_save_bills_skips_duplicates(tmp_path):
    json_path = str(tmp_path / "bills.json")
    db_path = str(tmp_path / "test.db")

    bills = [
        {
            "title": "Проект Закону про тест",
            "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/200",
            "page": 1,
        }
    ]

    stats1 = legal_monitor.save_bills(bills, json_path=json_path, db_path=db_path)
    assert stats1["new"] == 1

    stats2 = legal_monitor.save_bills(bills, json_path=json_path, db_path=db_path)
    assert stats2["new"] == 0
    assert stats2["total"] == 1


def test_save_bills_skips_bills_without_valid_id(tmp_path):
    json_path = str(tmp_path / "bills.json")
    db_path = str(tmp_path / "test.db")

    bills = [
        {"title": "Bad bill", "link": "https://example.com/noid", "page": 1},
    ]

    stats = legal_monitor.save_bills(bills, json_path=json_path, db_path=db_path)
    assert stats["new"] == 0
    assert stats["total"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Volumes/PASSPORT/projects/tg-ai-summarize-bot
pytest tests/test_legal_monitor.py -v
```

Expected: `ModuleNotFoundError: No module named 'legal_monitor'`

- [ ] **Step 3: Create legal_monitor.py with _extract_id and save_bills**

Create `legal_monitor.py`:

```python
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
    conn = duckdb.connect(db_path)
    _ensure_schema(conn)

    new_count = 0
    valid_bills: list[dict] = []

    for bill in bills:
        bill_id = _extract_id(bill.get("link", ""))
        if bill_id is None:
            logger.warning("Skipping bill with no valid ID: %s", bill.get("link"))
            continue

        existing = conn.execute(
            "SELECT id FROM bills WHERE id = ?", [bill_id]
        ).fetchone()

        scraped_at = now.isoformat()

        if existing is None:
            conn.execute(
                "INSERT INTO bills (id, title, link, scraped_at) VALUES (?, ?, ?, ?)",
                [bill_id, bill["title"], bill["link"], now],
            )
            new_count += 1

        valid_bills.append({
            "id": bill_id,
            "title": bill["title"],
            "link": bill["link"],
            "scraped_at": scraped_at,
        })

    # Load existing bills not in this batch and merge for JSON
    all_rows = conn.execute(
        "SELECT id, title, link, scraped_at FROM bills ORDER BY id DESC"
    ).fetchall()
    conn.close()

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

    total = len(all_bills_for_json)
    logger.info("save_bills: %d new, %d total", new_count, total)
    return {"new": new_count, "total": total}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_legal_monitor.py -v
```

Expected output:
```
tests/test_legal_monitor.py::test_extract_id_from_card_link PASSED
tests/test_legal_monitor.py::test_extract_id_returns_none_for_invalid_link PASSED
tests/test_legal_monitor.py::test_save_bills_writes_json_and_returns_stats PASSED
tests/test_legal_monitor.py::test_save_bills_skips_duplicates PASSED
tests/test_legal_monitor.py::test_save_bills_skips_bills_without_valid_id PASSED
```

- [ ] **Step 5: Commit**

```bash
git add legal_monitor.py tests/test_legal_monitor.py
git commit -m "feat: add legal_monitor save_bills and helpers with tests"
```

---

### Task 3: Scraping — scrape_bills and run

**Files:**
- Modify: `legal_monitor.py`
- Modify: `tests/test_legal_monitor.py`

- [ ] **Step 1: Write failing tests for scrape_bills and run**

Append to `tests/test_legal_monitor.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _make_playwright_mock(pages_data: list[list[dict]], max_page: int = 1):
    """
    Build a mock playwright context that returns pages_data sequentially.
    pages_data: list of pages, each page is list of {"title": str, "link": str}
    """
    page_mock = AsyncMock()

    call_count = {"n": 0}

    async def fake_evaluate(script):
        if "page-link" in script or "maxVal" in script:
            return str(max_page)
        idx = call_count["n"]
        call_count["n"] += 1
        if idx < len(pages_data):
            return pages_data[idx]
        return []

    page_mock.evaluate = fake_evaluate
    page_mock.goto = AsyncMock()
    page_mock.wait_for_timeout = AsyncMock()

    browser_mock = AsyncMock()
    browser_mock.new_page = AsyncMock(return_value=page_mock)

    chromium_mock = MagicMock()
    chromium_mock.launch = AsyncMock(return_value=browser_mock)

    playwright_mock = AsyncMock()
    playwright_mock.chromium = chromium_mock
    playwright_mock.__aenter__ = AsyncMock(return_value=playwright_mock)
    playwright_mock.__aexit__ = AsyncMock(return_value=False)

    return playwright_mock


def test_scrape_bills_returns_bills_from_pages():
    page1 = [
        {"title": "Законопроєкт А", "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/1"},
        {"title": "Законопроєкт Б", "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/2"},
    ]

    playwright_mock = _make_playwright_mock([page1], max_page=1)

    with patch("legal_monitor.async_playwright", return_value=playwright_mock):
        bills = asyncio.run(legal_monitor.scrape_bills(max_pages=1))

    assert len(bills) == 2
    assert bills[0]["title"] == "Законопроєкт А"
    assert bills[0]["page"] == 1


def test_scrape_bills_skips_empty_titles():
    page1 = [
        {"title": "Законопроєкт А", "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/1"},
        {"title": "", "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/2"},
    ]

    playwright_mock = _make_playwright_mock([page1], max_page=1)

    with patch("legal_monitor.async_playwright", return_value=playwright_mock):
        bills = asyncio.run(legal_monitor.scrape_bills(max_pages=1))

    assert len(bills) == 1


def test_run_returns_combined_stats(tmp_path):
    json_path = str(tmp_path / "bills.json")
    db_path = str(tmp_path / "test.db")

    page1 = [
        {"title": "Законопроєкт А", "link": "https://itd.rada.gov.ua/billinfo/Bills/Card/300"},
    ]
    playwright_mock = _make_playwright_mock([page1], max_page=1)

    with patch("legal_monitor.async_playwright", return_value=playwright_mock):
        stats = asyncio.run(
            legal_monitor.run(max_pages=1, json_path=json_path, db_path=db_path)
        )

    assert stats["new"] == 1
    assert stats["total"] == 1
```

- [ ] **Step 2: Run tests to verify new ones fail**

```bash
pytest tests/test_legal_monitor.py -v -k "scrape or run"
```

Expected: `ImportError` or `AttributeError: module 'legal_monitor' has no attribute 'scrape_bills'`

- [ ] **Step 3: Add scrape_bills and run to legal_monitor.py**

Append to `legal_monitor.py` (after the `save_bills` function):

```python
from playwright.async_api import async_playwright


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
    json_path: str = "data/bills.json",
    db_path: str | None = None,
) -> dict:
    """Scrape bills and persist them. Returns {"new": N, "total": M}."""
    bills = await scrape_bills(max_pages=max_pages)
    return save_bills(bills, json_path=json_path, db_path=db_path)
```

The import block at the top of `legal_monitor.py` should already include playwright from Task 2. If it doesn't, ensure the top of the file reads:

```python
import json
import logging
import re
from datetime import datetime, timezone

import duckdb
from playwright.async_api import async_playwright

import config
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_legal_monitor.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add legal_monitor.py tests/test_legal_monitor.py
git commit -m "feat: add scrape_bills and run to legal_monitor"
```

---

### Task 4: Bot command /bills

**Files:**
- Modify: `bot.py`

- [ ] **Step 1: Add import and handler to bot.py**

In `bot.py`, add `import legal_monitor` after the existing imports block (after `from llm import get_provider`):

```python
import legal_monitor
```

Then add the handler function after the `info` handler (before `handle_text`):

```python
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
            f"Знайдено {stats['total']} законопроєктів, збережено {stats['new']} нових."
        )
    except Exception as e:
        logger.error("bills_command failed: %s", e)
        await update.message.reply_text(f"Помилка: {e}")
```

- [ ] **Step 2: Register the handler in create_app**

In `create_app()`, add after the `info` handler line:

```python
app.add_handler(CommandHandler("bills", bills_command))
```

- [ ] **Step 3: Add /bills to the /start help text**

In the `start` handler, add to the help text block (after the `/info` line):

```python
            "/bills [pages] — scrape rada.gov.ua bills\n\n"
```

The full updated help text in `start`:

```python
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
            "/bills [pages] — scrape Verkhovna Rada bills"
        )
```

- [ ] **Step 4: Commit**

```bash
git add bot.py
git commit -m "feat: add /bills command to bot"
```

---

### Task 5: Scheduled daily job

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add scheduled_legal_monitor job to main.py**

In `main.py`, add two imports after the existing imports block (after `from bot import create_app`):

```python
from datetime import time as dt_time
import legal_monitor
```

Then in the `main()` async function, after the `await listener_module.setup(client, on_alert=on_alert)` line, add the inner function:

```python
    async def scheduled_legal_monitor(context) -> None:
        try:
            stats = await legal_monitor.run(max_pages=config.LEGAL_MONITOR_PAGES)
            logger.info(
                "Legal monitor: %d new bills, %d total", stats["new"], stats["total"]
            )
        except Exception as exc:
            logger.error("Scheduled legal monitor failed: %s", exc)
```

Then inside the `async with app:` block, after `await app.start()`, register the daily job:

```python
        lh, lm = config.LEGAL_MONITOR_TIME.split(":")
        app.job_queue.run_daily(
            scheduled_legal_monitor,
            time=dt_time(int(lh), int(lm)),
            job_kwargs={"misfire_grace_time": 7200},
        )
        logger.info("Legal monitor scheduled at %s UTC", config.LEGAL_MONITOR_TIME)
```

- [ ] **Step 2: Verify main.py runs without errors**

```bash
cd /Volumes/PASSPORT/projects/tg-ai-summarize-bot
python -c "import main" 2>&1 | head -20
```

Expected: no import errors (may show env var warnings which is fine).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: schedule daily legal monitor job in main.py"
```

---

### Task 6: Install playwright browser and smoke test

**Files:** (none — setup only)

- [ ] **Step 1: Install dependencies**

```bash
pip install playwright
playwright install chromium
```

- [ ] **Step 2: Run all unit tests**

```bash
pytest tests/test_legal_monitor.py -v
```

Expected: all 8 tests pass.

- [ ] **Step 3: Quick smoke test of save_bills**

```bash
python -c "
import legal_monitor, json
bills = [{'title': 'Test', 'link': 'https://itd.rada.gov.ua/billinfo/Bills/Card/99999', 'page': 1}]
stats = legal_monitor.save_bills(bills, json_path='/tmp/test_bills.json', db_path='/tmp/test.db')
print('Stats:', stats)
import json; print(json.load(open('/tmp/test_bills.json')))
"
```

Expected:
```
Stats: {'new': 1, 'total': 1}
[{'id': 99999, 'title': 'Test', 'link': '...', 'scraped_at': '...'}]
```

- [ ] **Step 4: Final commit if anything needed**

```bash
git add -p
git commit -m "chore: post-setup fixes"
```
