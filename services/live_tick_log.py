"""Tick-by-tick CSV recorder for TSMC's Live Arb universe: appends one row per
websocket book tick from services/live_warrant.py and services/live_options.py
to a local CSV, so the Live Arb tab's "Record"/"Download CSV" buttons
(app.py's /start_live_tick_log, /stop_live_tick_log, /live_tick_log_csv) can
capture a session for offline analysis. Recording is off by default and adds
no work per tick beyond an `is_active()` check until turned on."""
import csv
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")

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


def _today_path():
    return os.path.join(_DIR, f"tsmc_ticks_{datetime.now(TW_TZ):%Y%m%d}.csv")


def start():
    """Open (or resume) today's CSV file and begin recording. Safe to call
    while already active — a no-op in that case."""
    global _active, _file, _writer, _path, _started_at
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


def stop():
    """Flush and close the file handle; already-recorded rows are untouched
    and a later start() the same day resumes into the same file."""
    global _active, _file, _writer
    with _lock:
        if _file is not None:
            _file.flush()
            _file.close()
        _active = False
        _file = None
        _writer = None


def reset():
    """Stop recording (if active) and delete today's file entirely, clearing
    every captured row. Recording must be explicitly restarted via start()
    afterward — a reset never leaves the recorder silently running against a
    file that no longer exists.

    Closes the handle before deleting: Windows holds an exclusive lock on an
    open file, so os.remove() would fail if the recorder were still writing
    to it. Falls back to today's expected path when nothing has been started
    yet this process (e.g. right after a restart) but a same-day file from
    an earlier run is still on disk.
    """
    global _active, _file, _writer, _path, _rows_logged, _started_at
    with _lock:
        if _file is not None:
            _file.flush()
            _file.close()
        target = _path or _today_path()
        if os.path.exists(target):
            os.remove(target)
        _active = False
        _file = None
        _writer = None
        _path = None
        _rows_logged = 0
        _started_at = None


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


def record(row):
    """Append one tick row. No-op unless recording is active. `row` must
    supply exactly the COLUMNS keys (missing keys write as blank cells)."""
    global _rows_logged
    if not _active:
        return
    with _lock:
        if not _active or _writer is None:
            return
        _writer.writerow(row)
        _file.flush()
        _rows_logged += 1
