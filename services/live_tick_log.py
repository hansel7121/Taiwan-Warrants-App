"""Tick-by-tick CSV recorder for TSMC's Live Arb universe: appends one row per
websocket book tick (plus REST-seed and session-snapshot rows) from
services/live_warrant.py and services/live_options.py to a daily CSV. Started
and stopped automatically each trading day by services/scheduler.py (and still
by the Live Arb tab's Record/Stop buttons); the finished file is replayed at
end of day by scripts/eod_arb_replay.py, then gzipped and kept until prune().
Recording adds no work per tick beyond an `is_active()` check until turned on.
"""
import csv
import gzip
import os
import shutil
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")
UNDERLYING = "2330"

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "live_tick_logs")
# Point at a persistent volume in production — the container disk is wiped on redeploy.
_DIR = os.environ.get("LIVE_TICK_LOG_DIR") or _DEFAULT_DIR
COLUMNS = ["ts", "kind", "code", "name", "type", "strike", "exercise_ratio", "expiry", "dte",
           "bid", "ask", "bid_size", "ask_size", "src"]

# Rows are buffered and written by a daemon thread so a full-chain tick burst
# never pays a disk flush per row on the websocket callback thread.
FLUSH_S = 1.0
KEEP_DAYS = int(os.environ.get("TICK_LOG_KEEP_DAYS", "90"))
MAX_TOTAL_GB = float(os.environ.get("TICK_LOG_MAX_GB", "20"))

_lock = threading.Lock()
_active = False
_file = None
_writer = None
_path = None
_rows_logged = 0
_started_at = None
_buffer = []
_flusher = None
_flusher_stop = threading.Event()


def _stamp(d):
    return d.strftime("%Y%m%d")


def path_for(d):
    """Uncompressed CSV path for trading day `d`."""
    return os.path.join(_DIR, f"tsmc_ticks_{_stamp(d)}.csv")


def existing_path_for(d):
    """The day's file on disk — plain CSV or its gzipped form — or None."""
    for p in (path_for(d), path_for(d) + ".gz"):
        if os.path.exists(p):
            return p
    return None


def _today_path():
    return path_for(datetime.now(TW_TZ).date())


def _parse_day(name):
    """Trading date from a tick file name, or None if it isn't one."""
    if not name.startswith("tsmc_ticks_"):
        return None
    stem = name[len("tsmc_ticks_"):].split(".", 1)[0]
    try:
        return datetime.strptime(stem, "%Y%m%d").date()
    except ValueError:
        return None


def available_dates():
    """[{date, file, bytes}] for every tick file on disk, newest first."""
    out = []
    try:
        names = os.listdir(_DIR)
    except OSError:
        return out
    for name in names:
        d = _parse_day(name)
        if d is None:
            continue
        try:
            size = os.path.getsize(os.path.join(_DIR, name))
        except OSError:
            continue
        out.append({"date": d.isoformat(), "file": name, "bytes": size})
    return sorted(out, key=lambda r: r["date"], reverse=True)


def _flush_locked():
    """Write buffered rows to the open file. Caller holds `_lock`."""
    global _buffer, _rows_logged
    if not _buffer or _writer is None:
        return
    _writer.writerows(_buffer)
    _file.flush()
    _rows_logged += len(_buffer)
    _buffer = []


def _flush_loop():
    while not _flusher_stop.wait(FLUSH_S):
        try:
            with _lock:
                _flush_locked()
        except Exception as e:
            print(f"LIVETICKLOG: flush failed: {type(e).__name__}: {e}", flush=True)


def start():
    """Open (or resume) today's CSV file and begin recording. Safe to call
    while already active — a no-op in that case. Returns True if it started."""
    global _active, _file, _writer, _path, _started_at, _flusher
    with _lock:
        if _active:
            return False
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
        _flusher_stop.clear()
        _flusher = threading.Thread(target=_flush_loop, name="live-tick-log", daemon=True)
        _flusher.start()
    print(f"LIVETICKLOG: recording to {_path}", flush=True)
    return True


def _close_locked():
    global _active, _file, _writer
    if _file is not None:
        _flush_locked()
        _file.close()
    _active = False
    _file = None
    _writer = None
    _flusher_stop.set()


def stop():
    """Flush and close the file handle; already-recorded rows are untouched
    and a later start() the same day resumes into the same file."""
    with _lock:
        _close_locked()


def reset():
    """Stop recording and delete today's file entirely. Recording must be
    explicitly restarted via start() afterward."""
    global _path, _rows_logged, _started_at, _buffer
    with _lock:
        _close_locked()
        target = _path or _today_path()
        if os.path.exists(target):
            os.remove(target)
        _buffer = []
        _path = None
        _rows_logged = 0
        _started_at = None


def is_active():
    return _active


def status():
    with _lock:
        return {
            "active": _active,
            "rows_logged": _rows_logged + len(_buffer),
            "file": os.path.basename(_path) if _path else None,
            "started_at": _started_at,
        }


def current_path():
    """Path of the file being (or last) recorded, flushed so a download is current."""
    with _lock:
        _flush_locked()
        return _path


def record(row):
    """Buffer one tick row. No-op unless recording is active. `row` must
    supply the COLUMNS keys (missing keys write as blank cells)."""
    if not _active:
        return
    with _lock:
        if not _active:
            return
        _buffer.append(row)


def record_many(rows):
    """Buffer several rows at once — the session snapshot written on start()."""
    if not _active:
        return
    with _lock:
        if _active:
            _buffer.extend(rows)


def drop_stale_quote(row, book_ts):
    """Blank a snapshot row's prices when its book was last updated before today (Taipei):
    TW orders are day orders, so yesterday's quote is not on the book any more."""
    if book_ts is not None and book_ts.astimezone(TW_TZ).date() < datetime.now(TW_TZ).date():
        row = {**row, "bid": None, "ask": None, "bid_size": None, "ask_size": None}
    return row


def compress(d):
    """Gzip day `d`'s CSV in place (removing the plain file). Never touches
    the file currently being recorded. Returns the .gz path or None."""
    src = path_for(d)
    with _lock:
        if _active and _path == src:
            return None
    if not os.path.exists(src):
        return None
    dst = src + ".gz"
    with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.remove(src)
    return dst


def files_to_prune(files, today, keep_days, max_bytes):
    """Names of tick files to delete: older than `keep_days` calendar days,
    then oldest-first until the remainder fits in `max_bytes`. Today's file is
    never chosen. `files` is available_dates()'s shape."""
    drop = []
    kept = []
    for f in sorted(files, key=lambda r: r["date"]):
        age = (today - date.fromisoformat(f["date"])).days
        if f["date"] != today.isoformat() and age > keep_days:
            drop.append(f["file"])
        else:
            kept.append(f)
    total = sum(f["bytes"] for f in kept)
    for f in kept:
        if total <= max_bytes or f["date"] == today.isoformat():
            break
        drop.append(f["file"])
        total -= f["bytes"]
    return drop


def prune(keep_days=KEEP_DAYS, max_total_gb=MAX_TOTAL_GB):
    """Delete tick files outside the retention window / disk budget. Arb
    episodes live in Supabase, so this never loses a logged arb."""
    today = datetime.now(TW_TZ).date()
    names = files_to_prune(available_dates(), today, keep_days, max_total_gb * 1e9)
    for name in names:
        try:
            os.remove(os.path.join(_DIR, name))
            print(f"LIVETICKLOG: pruned {name}", flush=True)
        except OSError:
            pass
    return names
