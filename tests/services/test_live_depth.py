"""The web process's live-depth cache + poller (issue #51).

Same shape as test_live_price.py: the only faked boundary is db._run / the
supabase client, and the poll loop's error handling is pinned by calling its
body directly rather than spinning a thread up and killing it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import db
from services.broker import live_depth


TPE = timezone(timedelta(hours=8))


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table):
        self.table = table

    def execute(self):
        return _Result(list(self.table.rows))


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self.rows = store.rows.setdefault(name, [])

    def select(self, cols="*", **kw):
        self.store.selects.append((self.name, cols))
        return _FakeQuery(self)


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _FakeTable(self.store, name)


class _Store:
    def __init__(self, rows=None):
        self.rows = {"live_depth": list(rows or [])}
        self.selects = []


def _row(code="030001", ts="2026-08-04T13:29:59+08:00", broker="kgi",
         bid_prices=None, bid_volumes=None, ask_prices=None, ask_volumes=None):
    return {
        "code": code,
        "bid_prices": bid_prices or [1.20, 1.19, 1.18, 1.17, 1.16],
        "bid_volumes": bid_volumes or [10, 20, 30, 40, 50],
        "ask_prices": ask_prices or [1.21, 1.22, 1.23, 1.24, 1.25],
        "ask_volumes": ask_volumes or [5, 15, 25, 35, 45],
        "ts": ts,
        "broker": broker,
    }


@pytest.fixture
def store(monkeypatch):
    live_depth._cache.invalidate()
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    yield s
    live_depth._cache.invalidate()


# -- the poll ---------------------------------------------------------------

def test_a_poll_puts_every_row_in_the_cache(store):
    store.rows["live_depth"] = [_row("030001"), _row("030002")]

    assert live_depth._poll_once() == 2
    assert live_depth.entry("030001") is not None
    assert live_depth.entry("030002") is not None


def test_the_cached_row_carries_all_four_arrays(store):
    store.rows["live_depth"] = [_row(broker="fubon")]

    live_depth._poll_once()

    _stamp, value = live_depth.entry("030001")
    assert value["bid_prices"] == [1.20, 1.19, 1.18, 1.17, 1.16]
    assert value["bid_volumes"] == [10, 20, 30, 40, 50]
    assert value["ask_prices"] == [1.21, 1.22, 1.23, 1.24, 1.25]
    assert value["ask_volumes"] == [5, 15, 25, 35, 45]
    assert value["broker"] == "fubon"


def test_the_timestamp_is_parsed_to_an_aware_datetime(store):
    store.rows["live_depth"] = [_row(ts="2026-08-04T05:29:59+00:00")]

    live_depth._poll_once()

    ts = live_depth.entry("030001")[1]["ts"]
    assert isinstance(ts, datetime)
    assert ts.utcoffset() == timedelta(0)


def test_a_later_poll_overwrites_the_snapshot(store):
    store.rows["live_depth"] = [_row(bid_prices=[1.0, 0.9, 0.8, 0.7, 0.6])]
    live_depth._poll_once()

    store.rows["live_depth"] = [_row(bid_prices=[2.0, 1.9, 1.8, 1.7, 1.6])]
    live_depth._poll_once()

    assert live_depth.entry("030001")[1]["bid_prices"] == [2.0, 1.9, 1.8, 1.7, 1.6]


def test_a_code_missing_from_a_poll_keeps_its_last_snapshot(store):
    store.rows["live_depth"] = [_row("030001"), _row("030002")]
    live_depth._poll_once()

    store.rows["live_depth"] = [_row("030001")]
    live_depth._poll_once()

    assert live_depth.entry("030002") is not None


def test_an_empty_table_is_not_an_error(store):
    assert live_depth._poll_once() == 0


def test_the_poll_reads_only_live_depth(store):
    live_depth._poll_once()

    assert store.selects == [("live_depth", "*")]


# -- reads ------------------------------------------------------------------

def test_a_never_seen_code_has_no_entry(store):
    assert live_depth.entry("030999") is None


def test_snapshot_returns_the_values_for_seen_codes(store):
    store.rows["live_depth"] = [_row("030001"), _row("030002")]
    live_depth._poll_once()

    got = live_depth.snapshot(["030001", "030002"])

    assert set(got) == {"030001", "030002"}


def test_snapshot_skips_never_seen_codes(store):
    store.rows["live_depth"] = [_row("030001")]
    live_depth._poll_once()

    got = live_depth.snapshot(["030001", "030999"])

    assert list(got) == ["030001"]


# -- the loop's error handling -----------------------------------------------

def test_a_failing_poll_does_not_propagate(monkeypatch, store):
    def boom():
        raise RuntimeError("supabase down")

    monkeypatch.setattr(live_depth, "_poll_once", boom)

    live_depth._poll_guarded()   # must not raise


def test_a_failing_poll_leaves_the_cache_alone(monkeypatch, store):
    store.rows["live_depth"] = [_row()]
    live_depth._poll_once()

    monkeypatch.setattr(live_depth, "_poll_once",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    live_depth._poll_guarded()

    assert live_depth.entry("030001") is not None


# -- start() ------------------------------------------------------------------

def test_importing_the_module_starts_no_thread():
    assert live_depth._thread is None
