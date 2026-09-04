"""Tick-by-tick CSV recorder for TSMC's Live Arb universe: on its own
dirty-gated poll loop (independent of Direct Match/LP's own scan loops in
services/live_arb.py, so Record works whether or not either subtab's own
Start button has been clicked — same dirty-gate pattern, see that module's
`_combined_seq`), whenever anything in services/live_warrant.py's or
services/live_options.py's live books has changed since the last check, this
appends a FULL snapshot — every tracked warrant AND option's current best
bid/ask, not just whichever code ticked — as one row per code, all sharing
that snapshot's timestamp. Filtering the CSV to one `ts` value therefore
reconstructs the entire order-book state at that instant.

Wired to the Live Arb tab's Record/Download CSV/Reset CSV buttons
(app.py's /start_live_tick_log, /stop_live_tick_log, /reset_live_tick_log,
/live_tick_log_csv). Recording is off by default.

Volume warning: a full-universe snapshot on every detected change is far
heavier than logging single ticks — roughly (tracked codes) rows per
snapshot instead of 1. See POLL_INTERVAL_S below before recording a full
trading day.
"""
import csv
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from services import live_options, live_warrant

TW_TZ = ZoneInfo("Asia/Taipei")
UNDERLYING = "2330"

# Same cadence Direct Match's own scan loop polls at (services/live_arb.py's
# POLL_INTERVAL_S) — polling faster than the app itself ever reacts to a tick
# would just detect the same combined-seq change more often, not capture
# anything extra, since several ticks landing within one interval still
# collapse into a single detected change (and therefore one snapshot).
POLL_INTERVAL_S = 0.15

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "live_tick_logs")
COLUMNS = ["ts", "kind", "code", "name", "type", "strike", "exercise_ratio", "expiry", "dte",
           "bid", "ask", "bid_size", "ask_size", "src"]

_lock = threading.Lock()
_active = False
_file = None
_writer = None
_path = None
_rows_logged = 0
_started_at = None
_last_seq = None
_thread = None
_stop_event = threading.Event()


def _today_path():
    return os.path.join(_DIR, f"tsmc_ticks_{datetime.now(TW_TZ):%Y%m%d}.csv")


def _combined_seq():
    """Mirrors services/live_arb.py's function of the same name — a cheap
    "has anything changed" signal summed across both live caches, without
    importing live_arb.py itself (recording must work independently of
    whether either of its scan loops is running)."""
    w_seq, warrant_rows = live_warrant.snapshot_for_underlying(UNDERLYING)
    o_seq, option_rows = live_options.snapshot_for_underlying(UNDERLYING)
    return w_seq + o_seq, warrant_rows, option_rows


def _warrant_row(ts_str, today, r):
    maturity = r.get("maturity")
    best = r.get("best") or {}
    return {
        "ts": ts_str, "kind": "warrant", "code": r["code"], "name": r.get("name"),
        "type": r.get("type"), "strike": r.get("strike"), "exercise_ratio": r.get("exercise_ratio"),
        "expiry": maturity.isoformat() if maturity else None,
        "dte": (maturity - today).days if maturity else None,
        "bid": best.get("bid"), "ask": best.get("ask"),
        "bid_size": best.get("bid_size"), "ask_size": best.get("ask_size"),
        "src": r.get("src"),
    }


def _option_row(ts_str, today, r):
    expiry = r.get("expiry")
    best = r.get("best") or {}
    return {
        "ts": ts_str, "kind": "option", "code": r["code"], "name": r.get("name"),
        "type": r.get("type"), "strike": r.get("strike"), "exercise_ratio": None,
        "expiry": expiry.isoformat() if expiry else None,
        "dte": (expiry - today).days if expiry else None,
        "bid": best.get("bid"), "ask": best.get("ask"),
        "bid_size": best.get("bid_size"), "ask_size": best.get("ask_size"),
        "src": r.get("src"),
    }


def _write_snapshot(warrant_rows, option_rows):
    global _rows_logged
    now = datetime.now(TW_TZ)
    ts_str = now.isoformat(timespec="milliseconds")
    today = now.date()
    with _lock:
        if _writer is None:
            return
        for r in warrant_rows:
            _writer.writerow(_warrant_row(ts_str, today, r))
        for r in option_rows:
            _writer.writerow(_option_row(ts_str, today, r))
        _file.flush()
        _rows_logged += len(warrant_rows) + len(option_rows)


def _record_loop():
    global _last_seq
    while not _stop_event.is_set():
        try:
            seq, warrant_rows, option_rows = _combined_seq()
            changed = _last_seq is None or seq != _last_seq
            _last_seq = seq
            if changed and (warrant_rows or option_rows):
                _write_snapshot(warrant_rows, option_rows)
        except Exception as e:
            print(f"LIVETICKLOG: record loop failed: {type(e).__name__}: {e}", flush=True)
        _stop_event.wait(POLL_INTERVAL_S)


def start():
    """Open (or resume) today's CSV file and start the recording loop. Safe
    to call while already active — a no-op in that case."""
    global _active, _file, _writer, _path, _started_at, _last_seq, _thread
    with _lock:
        if _active:
            return
        os.makedirs(_DIR, exist_ok=True)
        _path = _today_path()
        is_new = not os.path.exists(_path) or os.path.getsize(_path) == 0
        _file = open(_path, "a", newline="", encoding="utf-8")
        _writer = csv.DictWriter(_file, fieldnames=COLUMNS)
        if is_new:
            _writer.writeheader()
            _file.flush()
        _active = True
        _started_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
        _last_seq = None
    _stop_event.clear()
    _thread = threading.Thread(target=_record_loop, daemon=True)
    _thread.start()


def stop():
    """Stop the recording loop and close the file handle; already-recorded
    rows are untouched and a later start() the same day resumes into the
    same file. Joins the loop thread before touching the file handle it
    owns, and releases `_lock` while joining so the loop's own in-flight
    `_write_snapshot` (which needs that same lock) can't deadlock against it.
    """
    global _active, _file, _writer, _thread
    with _lock:
        if not _active:
            return
        _active = False
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=POLL_INTERVAL_S * 10)
        _thread = None
    with _lock:
        if _file is not None:
            _file.flush()
            _file.close()
        _file = None
        _writer = None


def reset():
    """Stop recording (if active) and delete today's file entirely, clearing
    every captured row. Recording must be explicitly restarted via start()
    afterward.

    Falls back to today's expected path when nothing has been started yet
    this process (e.g. right after a restart) but a same-day file from an
    earlier run is still on disk. stop() closes the handle first: Windows
    holds an exclusive lock on an open file, so os.remove() would fail
    otherwise.
    """
    global _path, _rows_logged, _started_at, _last_seq
    stop()
    with _lock:
        target = _path or _today_path()
        if os.path.exists(target):
            os.remove(target)
        _path = None
        _rows_logged = 0
        _started_at = None
        _last_seq = None


def is_active():
    return _active


def status():
    with _lock:
        return {
            "active": _active,
            "rows_logged": _rows_logged,
            "file": os.path.basename(_path) if _path else None,
            "started_at": _started_at,
        }


def current_path():
    with _lock:
        return _path
