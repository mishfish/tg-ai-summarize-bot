from datetime import datetime, timezone, timedelta
import pytest
import alert_map


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "test.db")


def test_save_alert_returns_true_for_new(db):
    date = datetime.now(timezone.utc)
    result = alert_map.save_alert(1, "ch", "Тривога Пентагон", date, db_path=db)
    assert result is True


def test_save_alert_returns_false_for_duplicate(db):
    date = datetime.now(timezone.utc)
    alert_map.save_alert(1, "ch", "Тривога Пентагон", date, db_path=db)
    result = alert_map.save_alert(1, "ch", "Тривога Пентагон", date, db_path=db)
    assert result is False


def test_save_alert_same_msg_id_different_channel(db):
    date = datetime.now(timezone.utc)
    alert_map.save_alert(1, "ch_a", "text", date, db_path=db)
    result = alert_map.save_alert(1, "ch_b", "text", date, db_path=db)
    assert result is True  # different channel → not a duplicate


def test_get_alerts_empty(db):
    assert alert_map.get_alerts(db_path=db) == []


def test_get_alerts_returns_saved(db):
    date = datetime.now(timezone.utc)
    alert_map.save_alert(42, "src", "Тривога", date, db_path=db)
    rows = alert_map.get_alerts(db_path=db)
    assert len(rows) == 1
    assert rows[0]["text"] == "Тривога"
    assert rows[0]["channel"] == "src"
    assert rows[0]["msg_id"] == 42


def test_get_alerts_since_filter(db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    alert_map.save_alert(1, "ch", "old", old, db_path=db)
    alert_map.save_alert(2, "ch", "new", now, db_path=db)
    result = alert_map.get_alerts(since=now - timedelta(days=1), db_path=db)
    assert len(result) == 1
    assert result[0]["text"] == "new"


def test_get_alerts_ordered_newest_first(db):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    alert_map.save_alert(1, "ch", "first", t1, db_path=db)
    alert_map.save_alert(2, "ch", "second", t2, db_path=db)
    rows = alert_map.get_alerts(db_path=db)
    assert rows[0]["text"] == "second"
