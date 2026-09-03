"""Tick recorder (services/tick_recorder.py): the pure filename/retention/row
helpers, plus one record -> flush -> read round trip against a tmp directory.

The writer thread and the market gate are stubbed out rather than exercised —
what is worth pinning here is that a books frame becomes exactly one CSV row,
that a mid-day restart appends instead of truncating, and that retention never
deletes a day it should have kept.
"""
import gzip

import pytest

from services import tick_recorder as tr


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """Recorder writing into tmp_path, market forced open, writer thread off
    (flush_now is driven by hand so the tests stay deterministic)."""
    monkeypatch.setattr(tr, "TICK_DIR", str(tmp_path))
    monkeypatch.setattr(tr, "ENABLED", True)
    monkeypatch.setattr(tr, "_writer_started", True)
    monkeypatch.setattr(tr, "_market_open_fn", lambda market: True)
    monkeypatch.setattr(tr, "_gate_at", 0.0)
    monkeypatch.setattr(tr, "_handles", {})
    monkeypatch.setattr(tr, "_buffers", {s: [] for s in tr.STREAMS})
    monkeypatch.setattr(tr, "_dropped", {s: 0 for s in tr.STREAMS})
    monkeypatch.setattr(tr, "_written", {s: 0 for s in tr.STREAMS})
    return tr


def _read(rec, stream, day):
    with gzip.open(rec.path_for(stream, day), "rt") as fh:
        return fh.read()


# ── Pure helpers ───────────────────────────────────────────────────────────
def test_file_name_round_trips():
    assert tr.file_name("warrant", "2026-09-03") == "warrant-2026-09-03.csv.gz"
    assert tr.parse_file_name("warrant-2026-09-03.csv.gz") == ("warrant", "2026-09-03")


@pytest.mark.parametrize("name", [
    "warrant-2026-09-03.csv",      # not gzipped
    "notes.csv.gz",                # no stream/day
    "spot-2026-09-03.csv.gz",      # unknown stream
    "warrant-2026-09.csv.gz",      # malformed day
])
def test_parse_file_name_rejects_non_tick_files(name):
    assert tr.parse_file_name(name) is None


def test_format_row_leaves_a_missing_side_empty():
    assert tr.format_row(1, "038888", 1.23, 50, None, None) == "1,038888,1.23,50,,\n"


def test_stale_files_keeps_the_newest_days_present():
    names = [tr.file_name("warrant", d) for d in
             ("2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02")]
    # 09-03 has no file yet but still counts as a kept day.
    assert tr.stale_files(names, "2026-09-03", 3) == [
        tr.file_name("warrant", "2026-08-28"),
        tr.file_name("warrant", "2026-08-31"),
    ]


def test_stale_files_counts_days_present_not_calendar_days():
    """A long weekend must not expire the last session that actually traded."""
    names = [tr.file_name("warrant", d) for d in ("2026-08-28", "2026-08-31")]
    assert tr.stale_files(names, "2026-09-03", 3) == []


def test_stale_files_ignores_foreign_files(tmp_path):
    assert tr.stale_files(["README.md", "warrant-2026-09-03.csv.gz"], "2026-09-03", 1) == []


# ── Record -> flush -> read ────────────────────────────────────────────────
def test_record_writes_one_row_per_frame(rec):
    day = rec.taipei_day()
    rec.record("warrant", "038888", [{"price": 1.23, "size": 50}], [{"price": 1.24, "size": 30}])
    rec.record("warrant", "038889", [], [{"price": 9.9, "size": 1}])
    rec.flush_now()

    lines = _read(rec, "warrant", day).splitlines()
    assert lines[0] == "ts_ms,code,bid,bid_size,ask,ask_size"
    assert len(lines) == 3
    assert lines[1].split(",")[1:] == ["038888", "1.23", "50", "1.24", "30"]
    assert lines[2].split(",")[1:] == ["038889", "", "", "9.9", "1"]


def test_streams_are_separate_files(rec):
    day = rec.taipei_day()
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.record("option", "TXO202609C1000", [{"price": 2.0, "size": 2}], [])
    rec.flush_now()
    assert "038888" in _read(rec, "warrant", day)
    assert "038888" not in _read(rec, "option", day)


def test_reopening_mid_day_appends_rather_than_truncating(rec):
    """A restart writes a second gzip member; every decoder concatenates them,
    so the morning's ticks survive an afternoon redeploy."""
    day = rec.taipei_day()
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.flush_now()
    rec._handles["warrant"]["fh"].close()
    rec._handles.clear()

    rec.record("warrant", "038889", [{"price": 2.0, "size": 2}], [])
    rec.flush_now()

    body = _read(rec, "warrant", day)
    assert body.count("ts_ms,code") == 1     # header written once, not per member
    assert "038888" in body and "038889" in body


def test_closed_market_records_nothing(rec, monkeypatch):
    monkeypatch.setattr(rec, "_market_open_fn", lambda market: False)
    monkeypatch.setattr(rec, "_gate_at", 0.0)
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.flush_now()
    assert rec._buffers["warrant"] == []
    assert rec.available()["warrant"] == []


def test_full_buffer_drops_instead_of_growing(rec, monkeypatch):
    monkeypatch.setattr(rec, "MAX_BUFFER", 2)
    for _ in range(5):
        rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    assert len(rec._buffers["warrant"]) == 2
    assert rec._dropped["warrant"] == 3


def test_record_never_raises_on_a_malformed_frame(rec):
    """The recorder hangs off the websocket callback — a bad frame must not
    propagate into the SDK's message loop and kill the feed."""
    rec.record("warrant", "038888", [{"price": 1.0}], "not-a-list")
    rec.record("warrant", "038888", ["not-a-dict"], [])
    rec.flush_now()


def test_disabled_recorder_is_inert(rec, monkeypatch):
    monkeypatch.setattr(rec, "ENABLED", False)
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    assert rec._buffers["warrant"] == []


# ── Read side ──────────────────────────────────────────────────────────────
def test_resolve_day_defaults_to_the_newest_file(rec):
    day = rec.taipei_day()
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.flush_now()
    assert rec.resolve_day("warrant") == day
    assert rec.resolve_day("warrant", day) == day
    assert rec.resolve_day("warrant", "1999-01-01") is None
    assert rec.resolve_day("option") is None


def test_iter_csv_streams_the_decompressed_file(rec):
    day = rec.taipei_day()
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.flush_now()
    body = b"".join(rec.iter_csv("warrant", day)).decode()
    assert body == _read(rec, "warrant", day)
    assert b"".join(rec.iter_raw("warrant", day))[:2] == b"\x1f\x8b"   # gzip magic


def test_status_reports_files_and_gate(rec):
    rec.record("warrant", "038888", [{"price": 1.0, "size": 1}], [])
    rec.flush_now()
    st = rec.status()
    assert st["enabled"] and st["recording"]
    assert st["written"]["warrant"] == 1
    assert st["files"]["warrant"][0]["day"] == rec.taipei_day()
    assert st["files"]["warrant"][0]["bytes"] > 0


def test_concurrent_flushes_do_not_corrupt_the_file(rec, monkeypatch):
    """flush_now() is called by BOTH the writer thread and the download route.
    Unserialized they both see the file as absent, open two handles, each write
    a header, and interleave gzip members.

    The `os.path.exists` delay is what makes this deterministic: the real race
    window is a few microseconds wide and the GIL hides it, so without the
    delay this test passes with the lock removed and pins nothing.
    """
    import threading
    import time as _time

    day = rec.taipei_day()
    real_exists = rec.os.path.exists

    def slow_exists(path):
        out = real_exists(path)
        _time.sleep(0.02)
        return out

    monkeypatch.setattr(rec.os.path, "exists", slow_exists)

    errors = []

    def worker(n):
        try:
            for i in range(20):
                rec.record("warrant", f"03{n}{i:03d}",
                           [{"price": 1.0 + i, "size": 1}], [{"price": 2.0, "size": 2}])
                rec.flush_now()
        except Exception as e:  # pragma: no cover - only fires on a regression
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec.flush_now()

    assert errors == []
    body = _read(rec, "warrant", day)          # raises if any member is malformed
    lines = body.splitlines()
    assert lines.count("ts_ms,code,bid,bid_size,ask,ask_size") == 1
    assert len(lines) == 6 * 20 + 1
