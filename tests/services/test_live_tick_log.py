"""Tick recorder file handling (services/live_tick_log.py): buffered writes,
resume into the same day's file, gzip after the EOD replay, and retention."""
import csv
import gzip
from datetime import date

import pytest

from services import live_tick_log as tl


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "_DIR", str(tmp_path))
    yield tl
    tl.stop()


def _row(code, bid):
    return {"ts": "2026-10-07T09:00:00.000+08:00", "kind": "option", "code": code, "bid": bid, "src": "ws"}


def test_buffered_rows_reach_disk_on_stop_and_resume_appends(rec):
    assert rec.start() is True
    assert rec.start() is False          # already running
    rec.record(_row("A", 1.0))
    rec.record_many([_row("B", 2.0), _row("C", 3.0)])
    assert rec.status()["rows_logged"] == 3
    rec.stop()
    rec.record(_row("D", 4.0))           # ignored while stopped
    rec.start()
    rec.record(_row("E", 5.0))
    path = rec.current_path()             # flushes
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["code"] for r in rows] == ["A", "B", "C", "E"]   # one header, resumed file


def test_compress_skips_active_file_then_gzips(rec):
    rec.start()
    rec.record(_row("A", 1.0))
    today = date.fromisoformat(rec.available_dates()[0]["date"])
    assert rec.compress(today) is None   # still recording
    rec.stop()
    gz = rec.compress(today)
    assert gz.endswith(".gz") and rec.existing_path_for(today) == gz
    with gzip.open(gz, "rt") as fh:
        assert "A" in fh.read()


def test_files_to_prune_age_then_budget():
    today = date(2026, 10, 7)
    files = [{"date": "2026-06-01", "file": "old.csv.gz", "bytes": 10},
             {"date": "2026-10-05", "file": "a.csv.gz", "bytes": 60},
             {"date": "2026-10-06", "file": "b.csv.gz", "bytes": 60},
             {"date": "2026-10-07", "file": "today.csv", "bytes": 500}]
    assert tl.files_to_prune(files, today, keep_days=90, max_bytes=10_000) == ["old.csv.gz"]
    # Over budget: oldest first, never today's file.
    assert tl.files_to_prune(files, today, keep_days=90, max_bytes=100) == ["old.csv.gz", "a.csv.gz", "b.csv.gz"]


def test_drop_stale_quote_blanks_previous_day_books():
    from datetime import datetime, timedelta, timezone
    row = {"code": "A", "bid": 1.0, "ask": 1.1, "bid_size": 5, "ask_size": 6}
    now = datetime.now(timezone.utc)
    assert tl.drop_stale_quote(row, now) == row
    assert tl.drop_stale_quote(row, None) == row
    stale = tl.drop_stale_quote(row, now - timedelta(days=3))
    assert stale["bid"] is None and stale["ask"] is None and stale["code"] == "A"
