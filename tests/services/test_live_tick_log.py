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


def test_feed_watchdog_reconnects_a_silent_feed_mid_session_only(monkeypatch):
    """A pool that stops delivering frames is reconnected, but not at the open, the close, or forever."""
    from datetime import datetime
    from services import scheduler
    calls = []
    silence = {"s": 600.0}
    for mod in (scheduler.live_warrant, scheduler.live_options):
        monkeypatch.setattr(mod, "seconds_since_last_tick", lambda: silence["s"])
    monkeypatch.setattr(scheduler, "_restore_feed", lambda pool: calls.append(pool))
    monkeypatch.setattr(scheduler, "_feed_reconnects", {})
    day = datetime(2026, 10, 13)

    assert scheduler.check_feed("warrant", day.replace(hour=9, minute=0)) is False    # before the window
    assert scheduler.check_feed("warrant", day.replace(hour=13, minute=25)) is False  # closing auction
    silence["s"] = 30.0
    assert scheduler.check_feed("warrant", day.replace(hour=10)) is False             # ticking normally
    silence["s"] = None
    assert scheduler.check_feed("warrant", day.replace(hour=10)) is False             # no session up
    silence["s"] = 600.0
    results = [scheduler.check_feed("warrant", day.replace(hour=10, minute=m)) for m in (0, 5, 10, 15)]
    assert results == [True, True, True, False]                                       # capped per day
    assert scheduler.check_feed("option", day.replace(hour=10)) is True               # separate budget per pool
    assert calls == ["warrant"] * 3 + ["option"]


def test_preopen_probe_reconnects_a_pool_the_server_does_not_confirm(monkeypatch):
    """A pool whose socket never answers the subscriptions query is reconnected and re-probed; a healthy one is left alone."""
    from datetime import datetime
    from services import scheduler
    calls = []
    healthy = {"connections": 1, "replied": 1, "expected": 130, "missing": 0, "unsupported": False}
    dead = {**healthy, "replied": 0, "missing": 130}
    option_probes = iter([dead, healthy])
    monkeypatch.setattr(scheduler.live_warrant, "probe_subscriptions", lambda: healthy)
    monkeypatch.setattr(scheduler.live_options, "probe_subscriptions", lambda: next(option_probes))
    monkeypatch.setattr(scheduler, "_restore_feed", lambda pool: calls.append(pool))
    monkeypatch.setattr(scheduler, "_feed_reconnects", {})
    monkeypatch.setattr(scheduler, "_feed_status", {})

    assert scheduler.verify_feeds(datetime(2026, 10, 15, 8, 44)) == {"warrant": True, "option": True}
    assert calls == ["option"]
    assert scheduler.feed_status()["option"]["preopen"]["ok"] is True


def test_probe_healthy_tolerates_a_few_rejected_codes_only():
    from logic import live_warrant_logic as lwl
    ok = {"connections": 4, "replied": 4, "expected": 1050, "missing": 3, "unsupported": False}
    assert lwl.probe_healthy(ok)
    assert not lwl.probe_healthy({**ok, "replied": 3})                 # one socket never answered
    assert not lwl.probe_healthy({**ok, "missing": 300})               # subscriptions lost server-side
    assert not lwl.probe_healthy({**ok, "connections": 0, "replied": 0})  # no session at all
    assert lwl.probe_healthy({"unsupported": True})                    # SDK can't ask: never fail on it


def test_option_probe_reads_the_servers_subscription_list(monkeypatch):
    """probe_subscriptions counts tracked codes the server's own list is missing, and a socket that never answers."""
    import json as _json
    from services import live_options as lo

    class _WS:
        def __init__(self, conn, answer):
            self.conn, self.answer = conn, answer

        def subscriptions(self):
            if self.answer is not None:
                lo._handle_message(self.conn, _json.dumps({"event": "subscriptions", "data": [
                    {"id": str(i), "channel": "books", "symbol": s} for i, s in enumerate(self.answer)]}))

    live = {"codes": {"A", "B"}, "sub_ids": {}, "index": 0}
    live["ws"] = _WS(live, ["A", "B"])
    monkeypatch.setattr(lo, "_connections", [live])
    assert lo.probe_subscriptions(timeout=0.5) == {
        "connections": 1, "replied": 1, "expected": 2, "missing": 0, "unsupported": False}

    lost = {"codes": {"A", "B"}, "sub_ids": {}, "index": 0}
    lost["ws"] = _WS(lost, ["A"])
    dead = {"codes": {"C"}, "sub_ids": {}, "index": 1}
    dead["ws"] = _WS(dead, None)
    monkeypatch.setattr(lo, "_connections", [lost, dead])
    assert lo.probe_subscriptions(timeout=0.2) == {
        "connections": 2, "replied": 1, "expected": 3, "missing": 2, "unsupported": False}


def test_option_seconds_since_last_tick_tracks_books_frames(monkeypatch):
    from services import live_options as lo
    monkeypatch.setattr(lo, "_connections", [])
    assert lo.seconds_since_last_tick() is None
    monkeypatch.setattr(lo, "_connections", [{"codes": set()}])
    monkeypatch.setattr(lo, "_last_ws_data", lo.time.monotonic() - 900)
    assert lo.seconds_since_last_tick() > 800
    lo._handle_message({}, json.dumps({"event": "data", "channel": lo.BOOKS_CHANNEL,
                                       "data": {"symbol": "NOT_TRACKED", "bids": [], "asks": []}}))
    assert lo.seconds_since_last_tick() < 5
    lo._books.pop("NOT_TRACKED", None)


def test_prune_expired_untracks_only_contracts_past_expiry(monkeypatch):
    from datetime import date
    from services import live_options as lo
    monkeypatch.setattr(lo, "_tracked", ["OLD", "TODAY", "LATER"])
    monkeypatch.setattr(lo, "_contracts", {"OLD": {"expiry": date(2026, 10, 14)},
                                           "TODAY": {"expiry": date(2026, 10, 15)},
                                           "LATER": {"expiry": date(2026, 11, 18)}})
    monkeypatch.setattr(lo, "_connections", [])
    assert lo.prune_expired(date(2026, 10, 15)) == 1
    assert lo._tracked == ["TODAY", "LATER"]


def test_seconds_since_last_tick_tracks_books_frames(monkeypatch):
    from services import live_warrant as lw
    monkeypatch.setattr(lw, "_connections", [])
    assert lw.seconds_since_last_tick() is None
    monkeypatch.setattr(lw, "_connections", [{"codes": set()}])
    monkeypatch.setattr(lw, "_last_ws_data", lw.time.monotonic() - 900)
    assert lw.seconds_since_last_tick() > 800
    lw._handle_message({}, json.dumps({"event": "data", "channel": lw.BOOKS_CHANNEL,
                                       "data": {"symbol": "NOT_TRACKED", "bids": [], "asks": []}}))
    assert lw.seconds_since_last_tick() < 5
