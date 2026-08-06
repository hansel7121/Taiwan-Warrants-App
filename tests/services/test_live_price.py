"""The web process's live-price cache + poller (issue #46).

Real in-process calls; the only faked boundary is db._run / the supabase client,
following test_watchlist.py's _FakeClient/_Store pair, so no Supabase request is
ever made. The fakes here are cut down to the one table and one operation this
module uses (`select *` on live_prices) — there is no capacity or multi-table
coupling to express.

Nothing here starts the real poll thread: `start()` is only ever called from
wsgi.py and app.py's __main__ block, and the loop's error handling is pinned by
calling its body directly instead of spinning a thread up and killing it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import db
from services.broker import live_price


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
        self.rows = {"live_prices": list(rows or [])}
        self.selects = []


def _row(code="030001", price=1.23, ts="2026-08-04T13:29:59+08:00", broker="kgi",
         qty=None):
    return {"code": code, "price": price, "ts": ts, "broker": broker, "qty": qty}


@pytest.fixture
def store(monkeypatch):
    """A fake live_prices table behind db._run, plus an empty cache per test.

    The cache is a module-level singleton (one per process, like every other
    TTLCache in the app), so it is emptied between tests rather than rebuilt —
    rebuilding would leak a new instance into ttl_cache's registry each time.
    """
    live_price._cache.invalidate()
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    yield s
    live_price._cache.invalidate()


# -- the poll ---------------------------------------------------------------

def test_a_poll_puts_every_row_in_the_cache(store):
    store.rows["live_prices"] = [_row("030001"), _row("030002", price=4.5)]

    assert live_price._poll_once() == 2

    assert live_price.entry("030001")[1]["price"] == 1.23
    assert live_price.entry("030002")[1]["price"] == 4.5


def test_the_cached_row_carries_price_ts_and_broker(store):
    store.rows["live_prices"] = [
        _row(price=2.5, ts="2026-08-04T13:29:59+08:00", broker="fubon")]

    live_price._poll_once()

    _stamp, value = live_price.entry("030001")
    assert value == {
        "price": 2.5,
        "ts": datetime(2026, 8, 4, 13, 29, 59, tzinfo=TPE),
        "broker": "fubon",
        "qty": None,
    }


def test_the_cached_row_carries_qty_when_present(store):
    """kgisuperpy ticks carry a traded quantity (issue #54); the poll must not
    drop it on the way into the cache the SSE endpoint reads from."""
    store.rows["live_prices"] = [_row(broker="kgi", qty=5000)]

    live_price._poll_once()

    assert live_price.entry("030001")[1]["qty"] == 5000


def test_the_timestamp_is_parsed_to_an_aware_datetime(store):
    """PostgREST hands back a string; a consumer wants a real datetime, and a
    naive one would be read as UTC and shift the trade by eight hours."""
    store.rows["live_prices"] = [_row(ts="2026-08-04T05:29:59+00:00")]

    live_price._poll_once()

    ts = live_price.entry("030001")[1]["ts"]
    assert isinstance(ts, datetime)
    assert ts.utcoffset() == timedelta(0)


def test_the_cache_stamp_is_the_tick_time_not_the_poll_time(store):
    """`ts` is authoritative over "when we happened to read the table", the same
    way _relay_tick prefers tick.ts over db._now()."""
    tick = datetime(2026, 8, 4, 13, 29, 59, tzinfo=TPE)
    store.rows["live_prices"] = [_row(ts=tick.isoformat())]

    live_price._poll_once()

    stamp, _value = live_price.entry("030001")
    assert stamp == tick.timestamp()


def test_a_later_poll_overwrites_the_price(store):
    store.rows["live_prices"] = [_row(price=1.23)]
    live_price._poll_once()

    store.rows["live_prices"] = [_row(price=9.99)]
    live_price._poll_once()

    assert live_price.entry("030001")[1]["price"] == 9.99


def test_a_code_missing_from_a_poll_keeps_its_last_tick(store):
    """Refresh, never clear-then-refill: a row the worker has not rewritten yet
    (or a hiccuping read) must not blank a price ADR-0005 wants left on screen."""
    store.rows["live_prices"] = [_row("030001"), _row("030002", price=4.5)]
    live_price._poll_once()

    store.rows["live_prices"] = [_row("030001", price=2.0)]
    live_price._poll_once()

    assert live_price.entry("030002")[1]["price"] == 4.5


def test_an_empty_table_is_not_an_error(store):
    assert live_price._poll_once() == 0


def test_the_poll_reads_only_live_prices(store):
    live_price._poll_once()

    assert store.selects == [("live_prices", "*")]


# -- reads ------------------------------------------------------------------

def test_a_never_seen_code_has_no_entry(store):
    """"Never received a tick" shows nothing (ADR-0005) — so it must be
    distinguishable from a code with a price, not a zero."""
    assert live_price.entry("030999") is None


def test_snapshot_returns_the_values_for_seen_codes(store):
    store.rows["live_prices"] = [_row("030001"), _row("030002", price=4.5)]
    live_price._poll_once()

    got = live_price.snapshot(["030001", "030002"])

    assert set(got) == {"030001", "030002"}
    assert got["030001"]["price"] == 1.23
    assert got["030002"]["broker"] == "kgi"


def test_snapshot_skips_never_seen_codes(store):
    store.rows["live_prices"] = [_row("030001")]
    live_price._poll_once()

    got = live_price.snapshot(["030001", "030999"])

    assert list(got) == ["030001"]


def test_a_stale_entry_is_still_served(store):
    """The read path is entry(), not fresh(): a tick older than the cache TTL
    stays visible, because staleness is decided by Worker Status
    (pool.live_status), not by how long ago the price printed."""
    old = datetime.now(timezone.utc) - timedelta(seconds=live_price._TTL_SECONDS * 2)
    store.rows["live_prices"] = [_row(ts=old.isoformat())]
    live_price._poll_once()

    assert live_price.entry("030001") is not None
    assert live_price._cache.fresh("030001") is None   # would have hidden it


# -- the loop's error handling ---------------------------------------------

def test_a_failing_poll_does_not_propagate(monkeypatch, store):
    """One bad read costs one interval, not the thread: nothing restarts this
    poller, so an escaping exception would freeze prices for the process's life."""
    def boom():
        raise RuntimeError("supabase down")

    monkeypatch.setattr(live_price, "_poll_once", boom)

    live_price._poll_guarded()   # must not raise


def test_a_failing_poll_leaves_the_cache_alone(monkeypatch, store):
    store.rows["live_prices"] = [_row(price=1.23)]
    live_price._poll_once()

    monkeypatch.setattr(live_price, "_poll_once",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    live_price._poll_guarded()

    assert live_price.entry("030001")[1]["price"] == 1.23


# -- start() ----------------------------------------------------------------

def test_importing_the_module_starts_no_thread():
    """Every test process imports app (hence this module) — none of them may
    end up with a thread polling real Supabase."""
    assert live_price._thread is None
