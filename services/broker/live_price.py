"""Web process's side of the worker->web live-price relay (issue #46).

broker_worker.py and the Flask app are separate Render services with no
shared memory, so a Tick can't reach the web process directly. The worker
upserts each tick into `live_prices`; this module polls that table into an
in-process TTLCache, which the SSE endpoint streams to browsers.

Polling, not Supabase Realtime: the sync supabase-py client this codebase
uses raises NotImplementedError from `.channel()` — Realtime needs the async
client, and there's no asyncio anywhere in this repo.

Reads use `TTLCache.entry()`, never `.fresh()`: per ADR-0005 a code's last
tick stays displayed after its connection drops (marked stale, not cleared).
Staleness for the UI comes from `pool.live_status()` (is the connection
`connected`?), not tick age, so this cache's TTL is just bookkeeping.

Cached value, keyed by warrant code:
{"price": float, "ts": datetime, "broker": str, "qty": int | None}

Importing this module starts nothing; `start()` is called explicitly from
wsgi.py and app.py's __main__ block.
"""
import logging
import os
import threading
from datetime import datetime

from logic.ttl_cache import TTLCache
from services import db

log = logging.getLogger(__name__)

TABLE = "live_prices"
POLL_SEC = float(os.environ.get("LIVE_PRICE_POLL_SEC", "1"))
_TTL_SECONDS = 3600

_cache = TTLCache("live_price", _TTL_SECONDS)

_thread = None
_start_lock = threading.Lock()
_stop_event = threading.Event()


def entry(code):
    """(epoch_float, {"price", "ts", "broker", "qty"}) for `code`, or None if
    never seen."""
    return _cache.entry(code)


def snapshot(codes):
    """{code: {"price", "ts", "broker", "qty"}} for those `codes` that have a tick."""
    out = {}
    for code in codes:
        hit = _cache.entry(code)
        if hit is not None:
            out[code] = hit[1]
    return out


def _poll_once():
    """Refresh the cache from one read of live_prices. Returns rows applied.

    Upsert-style: a code missing from this response keeps its last tick.
    """
    rows = db._run(lambda c: c.table(TABLE).select("*").execute()).data or []
    for row in rows:
        ts = _parse_ts(row["ts"])
        _cache.set(
            row["code"],
            {"price": row["price"], "ts": ts, "broker": row["broker"],
             "qty": row.get("qty")},
            ts=ts.timestamp(),
        )
    return len(rows)


def _parse_ts(raw):
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(raw)


def _poll_forever():
    while not _stop_event.is_set():
        _poll_guarded()
        _stop_event.wait(POLL_SEC)


def _poll_guarded():
    """One _poll_once() that logs instead of raising."""
    try:
        _poll_once()
    except Exception as e:
        log.error("live price poll failed: %s: %s", type(e).__name__, e,
                  exc_info=True)


def start():
    """Start the live-price poller exactly once per process."""
    global _thread
    with _start_lock:
        if _thread is not None:
            return _thread
        _stop_event.clear()
        _thread = threading.Thread(
            target=_poll_forever, name="live-price-poll", daemon=True)
        _thread.start()
        print(f"LIVE: price poller started (every {POLL_SEC}s)", flush=True)
        return _thread


def stop():
    """Stop the poll thread. Tests only."""
    global _thread
    with _start_lock:
        if _thread is None:
            return
        _stop_event.set()
        _thread.join(timeout=POLL_SEC + 5)
        _thread = None
