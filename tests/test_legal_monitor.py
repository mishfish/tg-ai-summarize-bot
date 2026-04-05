import json
import os
import tempfile

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

    with open(json_path) as f:
        saved = json.load(f)
    assert len(saved) == 1


def test_save_bills_skips_bills_without_valid_id(tmp_path):
    json_path = str(tmp_path / "bills.json")
    db_path = str(tmp_path / "test.db")

    bills = [
        {"title": "Bad bill", "link": "https://example.com/noid", "page": 1},
    ]

    stats = legal_monitor.save_bills(bills, json_path=json_path, db_path=db_path)
    assert stats["new"] == 0
    assert stats["total"] == 0


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
