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
