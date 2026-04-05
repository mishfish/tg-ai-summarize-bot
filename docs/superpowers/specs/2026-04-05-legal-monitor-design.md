# Legal Monitor Module — Design Spec

**Date:** 2026-04-05  
**Status:** Approved

## Overview

Rewrite the Go `legal-monitor` scraper as a Python module integrated into the existing Telegram bot project. The module scrapes Ukrainian parliament bills from `itd.rada.gov.ua/billinfo/Bills/period` and persists them to JSON and DuckDB. No Telegram notifications in this iteration.

## Architecture

Single file `legal_monitor.py` at the project root, following the existing module pattern (e.g., `summarizer.py`, `storage.py`).

### Public interface

```python
async def scrape_bills(max_pages: int = 0) -> list[dict]
def save_bills(bills: list[dict]) -> dict  # returns {"total": N, "new": M}
async def run(max_pages: int = 0) -> dict  # scrape + save, returns stats
```

## Scraping (Playwright)

- Uses `playwright` (async API, headless Chromium) — mirrors Go's chromedp approach
- Step 1: load page 1, detect max page count from pagination buttons
- Step 2: iterate pages, extract `tbody tr` rows → `td:nth-child(2) a` (link) + `td:nth-child(4)` (title)
- Retry up to 2 times per page on failure; 300ms delay between pages
- `max_pages=0` means scrape all pages; otherwise stop at that count

## Storage

### JSON
- Path: `data/bills.json`
- Format: `[{"id": 69567, "title": "...", "link": "...", "scraped_at": "..."}]`
- Full overwrite on each save (list of all known bills)

### DuckDB
- File: `data/storage.db` (shared with existing storage)
- Table: `bills (id INTEGER PRIMARY KEY, title TEXT, link TEXT, scraped_at TIMESTAMP)`
- Created if not exists; INSERT OR IGNORE to skip duplicates
- `id` extracted from link path: `.../Bills/Card/69567` → `69567`

### Return value of `save_bills`
```python
{"total": 1500, "new": 12}  # total in DB, how many were inserted this run
```

## Bot integration

### Command `/bills [pages]`
- Added to `bot.py`, requires auth
- Optional `pages` arg (int): limits pages scraped (default: all)
- Sends typing action, runs `legal_monitor.run(max_pages)`, replies: "Знайдено 1500 законопроєктів, збережено 12 нових"
- On error: replies with error message

### Scheduled job
- Added to `main.py` alongside existing `scheduled_summary`
- Runs daily at `LEGAL_MONITOR_TIME` UTC (configured in `.env`)
- Does not send a Telegram message — just scrapes and saves silently

## Config additions (`config.py`)

```python
LEGAL_MONITOR_TIME = os.getenv("LEGAL_MONITOR_TIME", "07:00")  # UTC
LEGAL_MONITOR_PAGES = int(os.getenv("LEGAL_MONITOR_PAGES", "0"))  # 0 = all
```

## Dependencies

Add to `requirements.txt`:
```
playwright
```

First-time setup: `playwright install chromium`

## Error handling

- Per-page errors are logged and skipped (same as Go version)
- If page detection fails, default to 50 pages (same as Go)
- DB/JSON errors are raised (not silently swallowed)

## Out of scope (this iteration)

- Telegram notifications for new bills
- Keyword filtering
- LLM summarization of bills
