"""Tick recorder file handling (services/live_tick_log.py): buffered writes,
resume into the same day's file, gzip after the EOD replay, and retention."""
import csv
import gzip
import json
import os
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


def test_record_start_snapshots_every_book_under_one_timestamp(rec, monkeypatch):
    """Record button and scheduler share one starter: every tracked book lands first, stamped alike."""
    from services import scheduler
    monkeypatch.setattr(scheduler.live_options, "snapshot_for_underlying", lambda u: (None, ["C1"]))
    monkeypatch.setattr(scheduler.live_warrant, "tick_rows_for_underlying",
                        lambda: [{**_row("W1", 1.0), "ts": "t1", "kind": "warrant", "src": "snapshot"}])
    monkeypatch.setattr(scheduler.live_options, "tick_rows_for_underlying",
                        lambda: [{**_row("C1", 2.0), "ts": "t2", "src": "snapshot"}])
    assert scheduler.ensure_tick_recording() is True
    assert scheduler.ensure_tick_recording() is False   # already running: no second snapshot
    with open(rec.current_path(), newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["code"] for r in rows] == ["W1", "C1"]
    assert rows[0]["ts"] == rows[1]["ts"] and rows[0]["ts"] not in ("t1", "t2")


def test_spot_ticks_go_to_their_own_file_and_follow_the_tick_file(rec):
    """2330 spot rows land in tsmc_spot_*.csv; compress and reset treat both files alike."""
    from datetime import datetime
    from services import live_warrant as lw
    lw._underlying_codes.add(rec.UNDERLYING)
    try:
        lw._handle_message({}, json.dumps({"event": "data", "channel": lw.BOOKS_CHANNEL, "data": {
            "symbol": rec.UNDERLYING, "bids": [{"price": 2450.0, "size": 10}], "asks": [{"price": 2455.0, "size": 8}]}}))
        rec.start()
        lw._handle_message({}, json.dumps({"event": "data", "channel": lw.BOOKS_CHANNEL, "data": {
            "symbol": rec.UNDERLYING, "bids": [{"price": 2450.0, "size": 10}], "asks": [{"price": 2455.0, "size": 8}]}}))
        assert lw.spot_rows_for_underlying()[0]["src"] == "snapshot"
    finally:
        lw._underlying_codes.discard(rec.UNDERLYING)
        lw._underlying_books.pop(rec.UNDERLYING, None)
    rec.record(_row("A", 1.0))
    with open(rec.current_path(spot=True), newline="") as fh:
        spot = list(csv.DictReader(fh))
    assert [(r["code"], r["bid"], r["ask"], r["mid"], r["src"]) for r in spot] == [("2330", "2450.0", "2455.0", "2452.5", "ws")]
    assert rec.status()["spot_rows_logged"] == 1
    rec.stop()
    today = datetime.now(rec.TW_TZ).date()
    rec.compress(today)
    assert rec.existing_path_for(today, spot=True).endswith("tsmc_spot_" + today.strftime("%Y%m%d") + ".csv.gz")
    assert rec.existing_path_for(today).endswith(".csv.gz")
    rec.start()
    rec.reset()
    assert not os.path.exists(rec.spot_path_for(today)) and not os.path.exists(rec.path_for(today))
