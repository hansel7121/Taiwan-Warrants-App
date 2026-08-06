"""Web process's side of the worker->web live-depth relay (issue #51).

Same shape as live_price.py: broker_worker.py upserts each Depth into
live_depth, and this module polls that table into an in-process TTLCache the
SSE endpoint reads. A separate poller and cache rather than folding depth into
live_price.py's, because the two tables and value shapes are unrelated beyond
sharing the poll-and-cache pattern.

Reads use `TTLCache.entry()`, never `.fresh()`, for the same reason live_price
does: a code's last snapshot stays displayed after its connection drops
(ADR-0005), staleness for the UI comes from Worker Status, not cache age.

Cached value, keyed by code (warrant or, since #55, TXO):
{"bid_prices": [float]*5, "bid_volumes": [int]*5,
 "ask_prices": [float]*5, "ask_volumes": [int]*5,
 "ts": datetime, "broker": str, "instrument": str}

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

TABLE = "live_depth"
POLL_SEC = float(os.environ.get("LIVE_DEPTH_POLL_SEC", "1"))
_TTL_SECONDS = 3600

_cache = TTLCache("live_depth", _TTL_SECONDS)

_thread = None
_start_lock = threading.Lock()
_stop_event = threading.Event()


def entry(code):
    """(epoch_float, {"bid_prices", "bid_volumes", "ask_prices", "ask_volumes", "ts", "broker", "instrument"}) for `code`, or None if never seen."""
    return _cache.entry(code)


def snapshot(codes):
    """{code: {...}} for those `codes` that have a depth snapshot."""
    out = {}
    for code in codes:
        hit = _cache.entry(code)
        if hit is not None:
            out[code] = hit[1]
    return out


def _poll_once():
    """Refresh the cache from one read of live_depth; a missing code keeps its last snapshot. Returns rows applied."""
    rows = db._run(lambda c: c.table(TABLE).select("*").execute()).data or []
    for row in rows:
        ts = _parse_ts(row["ts"])
        _cache.set(
            row["code"],
            {"bid_prices": row["bid_prices"], "bid_volumes": row["bid_volumes"],
             "ask_prices": row["ask_prices"], "ask_volumes": row["ask_volumes"],
             "ts": ts, "broker": row["broker"],
             "instrument": row.get("instrument", "warrant")},
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
        log.error("live depth poll failed: %s: %s", type(e).__name__, e,
                  exc_info=True)


def start():
    """Start the live-depth poller exactly once per process."""
    global _thread
    with _start_lock:
        if _thread is not None:
            return _thread
        _stop_event.clear()
        _thread = threading.Thread(
            target=_poll_forever, name="live-depth-poll", daemon=True)
        _thread.start()
        print(f"LIVE: depth poller started (every {POLL_SEC}s)", flush=True)
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
