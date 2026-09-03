"""Append-only tick log for the Live Warrant / Live Options websocket feeds.

`record()` is called from services/live_warrant.py and services/live_options.py
on every books frame, off the hot path's lock; it drops the tick's best level
into an in-memory buffer that a daemon thread flushes to a gzipped, one-row-
per-tick CSV on disk (one file per stream per Taipei trading day). app.py's
/live_tick_status and /live_tick_csv routes read those files back so the Live
tabs can offer an end-of-day download.

Long format, not wide: a books frame moves ONE contract, so a row-per-tick
snapshot of all ~1,200 contracts would repeat 1,199 unchanged cells per tick
(~350 GB/day at 3k ticks/s, versus ~2.4 GB raw / ~490 MB gzipped here). Pivot
to wide at analysis time, not at write time.

Nothing here may ever break quoting: record() swallows its own exceptions, and
a buffer that outruns the writer drops ticks (counted) rather than growing
without bound.
"""
import gzip
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")

# Streams are separate files so the instrument kind never has to be repeated as
# a column on tens of millions of rows.
STREAMS = ("warrant", "option")
HEADER = "ts_ms,code,bid,bid_size,ask,ask_size\n"

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENABLED = os.environ.get("TICK_RECORDER", "1") != "0"
# Point this at a mounted volume in Coolify — the container filesystem is wiped
# on every redeploy, so an unmounted path silently loses the day's ticks.
TICK_DIR = os.environ.get("TICK_RECORDER_DIR") or os.path.join(_BASE_DIR, "data", "ticks")
# "Last day" is the ask, but a forgotten download shouldn't be unrecoverable.
KEEP_DAYS = int(os.environ.get("TICK_RECORDER_KEEP_DAYS", "3"))
FLUSH_S = float(os.environ.get("TICK_RECORDER_FLUSH_S", "0.25"))
# ~3k ticks/s * 0.25s = ~750 rows per flush; this is a runaway backstop two
# orders of magnitude above that, not a tuning knob.
MAX_BUFFER = int(os.environ.get("TICK_RECORDER_MAX_BUFFER", "500000"))

_buf_lock = threading.Lock()
# Serializes the disk side. flush_now() is called by BOTH the writer thread and
# the download route; without this they can open two handles for the same file,
# write a duplicate header, and interleave two gzip members into garbage.
_flush_lock = threading.Lock()
_buffers = {s: [] for s in STREAMS}
_dropped = {s: 0 for s in STREAMS}
_written = {s: 0 for s in STREAMS}
_handles = {}  # stream -> {"day": "YYYY-MM-DD", "fh": GzipFile}

_writer_started = False
_writer_lock = threading.Lock()

# is_market_open lives in services/scheduler.py, which imports live_warrant,
# which imports this module — resolve it at call time to keep that cycle from
# forming at import time.
_market_open_fn = None
_gate_at = 0.0
_gate_open = False


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in tests/services/test_tick_recorder.py)
# ---------------------------------------------------------------------------
def taipei_day(now=None):
    """Today's Taipei date as YYYY-MM-DD — the trading day a tick belongs to."""
    return (now or datetime.now(_TZ_TAIPEI)).astimezone(_TZ_TAIPEI).strftime("%Y-%m-%d")


def file_name(stream, day):
    """`warrant-2026-09-03.csv.gz` — the on-disk name for one stream-day."""
    return f"{stream}-{day}.csv.gz"


def parse_file_name(name):
    """(stream, day) from a tick file name, or None if it isn't one."""
    if not name.endswith(".csv.gz"):
        return None
    stem = name[: -len(".csv.gz")]
    stream, _, day = stem.partition("-")
    if stream not in STREAMS or len(day) != 10:
        return None
    return stream, day


def stale_files(names, today, keep_days):
    """Tick files older than `keep_days` trading-day *names* back from `today`.

    Compares date strings, not calendar arithmetic: retention counts the files
    actually present, so a weekend or holiday gap never silently expires a day
    that was never written.
    """
    days = sorted({p[1] for p in map(parse_file_name, names) if p} | {today}, reverse=True)
    keep = set(days[:max(1, keep_days)])
    return sorted(n for n in names
                  if (p := parse_file_name(n)) and p[1] not in keep)


def format_row(ts_ms, code, bid, bid_size, ask, ask_size):
    """One CSV line; a missing side writes empty cells, never a placeholder."""
    def c(v):
        return "" if v is None else str(v)
    return f"{ts_ms},{code},{c(bid)},{c(bid_size)},{c(ask)},{c(ask_size)}\n"


# ---------------------------------------------------------------------------
# Hot path — called from the websocket callback thread, once per books frame
# ---------------------------------------------------------------------------
def _market_open():
    global _market_open_fn
    if _market_open_fn is None:
        from services.scheduler import is_market_open
        _market_open_fn = is_market_open
    # tw_equity is the narrower window and both Fubon sessions share it (same
    # gate services/scheduler.py uses to start them).
    return _market_open_fn("tw_equity")


def _open_now():
    """Market gate, re-evaluated at most once a second — this runs thousands of
    times a second and a fresh datetime/tz conversion per tick is pure waste."""
    global _gate_at, _gate_open
    now = time.time()
    if now - _gate_at >= 1.0:
        _gate_at = now
        try:
            _gate_open = _market_open()
        except Exception:
            _gate_open = False
    return _gate_open


def record(stream, code, bids, asks):
    """Buffer this frame's best bid/ask. Never raises — a recorder bug must not
    take down the quote feed it is attached to."""
    if not ENABLED or not code:
        return
    try:
        _ensure_writer()
        if not _open_now():
            return
        b = bids[0] if bids else None
        a = asks[0] if asks else None
        row = (
            int(time.time() * 1000), code,
            b.get("price") if b else None, b.get("size") if b else None,
            a.get("price") if a else None, a.get("size") if a else None,
        )
        with _buf_lock:
            buf = _buffers.get(stream)
            if buf is None:
                return
            if len(buf) >= MAX_BUFFER:
                _dropped[stream] += 1
                return
            buf.append(row)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------
def _ensure_writer():
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        _writer_started = True
        threading.Thread(target=_writer_loop, name="tick-recorder", daemon=True).start()


def _writer_loop():
    while True:
        time.sleep(FLUSH_S)
        try:
            flush_now()
        except Exception as e:
            print(f"TICKREC: flush failed: {e}", flush=True)


def _handle_for(stream, day):
    """The open file handle for this stream-day, rotating (and pruning) on a
    Taipei date change. Returns (handle, is_new_file)."""
    h = _handles.get(stream)
    if h and h["day"] == day:
        return h["fh"], False
    if h:
        try:
            h["fh"].close()
        except Exception:
            pass
    os.makedirs(TICK_DIR, exist_ok=True)
    path = os.path.join(TICK_DIR, file_name(stream, day))
    fresh = not os.path.exists(path)
    fh = open(path, "ab")
    _handles[stream] = {"day": day, "fh": fh}
    prune()
    return fh, fresh


def flush_now():
    """Drain every buffer to disk. Called on the flush interval and by the
    download route, so a file served mid-session is current to the last tick.

    Each flush appends one COMPLETE gzip member rather than writing into a
    single open gzip stream. A stream left open mid-day has no trailer, and
    Python's own reader refuses it ("Compressed file ended before the
    end-of-stream marker was reached") — which would make exactly the two
    cases that matter unreadable: downloading while the market is still open,
    and whatever the process was holding when it died. Members concatenate
    transparently for every decoder, at a cost of ~20 bytes per flush.
    """
    day = taipei_day()
    with _flush_lock:
        for stream in STREAMS:
            with _buf_lock:
                rows = _buffers[stream]
                if not rows:
                    continue
                _buffers[stream] = []
            fh, fresh = _handle_for(stream, day)
            payload = (HEADER if fresh else "") + "".join(format_row(*r) for r in rows)
            fh.write(gzip.compress(payload.encode()))
            fh.flush()
            _written[stream] += len(rows)


def prune():
    """Delete tick files outside the retention window."""
    try:
        names = os.listdir(TICK_DIR)
    except OSError:
        return
    for name in stale_files(names, taipei_day(), KEEP_DAYS):
        try:
            os.remove(os.path.join(TICK_DIR, name))
            print(f"TICKREC: pruned {name}", flush=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Read side — app.py's /live_tick_status and /live_tick_csv
# ---------------------------------------------------------------------------
def available():
    """{stream: [{day, bytes}, ...]} newest day first, for the download UI."""
    out = {s: [] for s in STREAMS}
    try:
        names = os.listdir(TICK_DIR)
    except OSError:
        return out
    for name in names:
        parsed = parse_file_name(name)
        if not parsed:
            continue
        stream, day = parsed
        try:
            size = os.path.getsize(os.path.join(TICK_DIR, name))
        except OSError:
            continue
        out[stream].append({"day": day, "bytes": size})
    for rows in out.values():
        rows.sort(key=lambda r: r["day"], reverse=True)
    return out


def status():
    """Recorder health for /live_tick_status — what is on disk plus whether the
    gate is currently letting ticks through."""
    with _buf_lock:
        buffered = {s: len(_buffers[s]) for s in STREAMS}
        dropped = dict(_dropped)
    return {
        "enabled": ENABLED,
        "recording": ENABLED and _open_now(),
        "dir": TICK_DIR,
        "keep_days": KEEP_DAYS,
        "buffered": buffered,
        "dropped": dropped,
        "written": dict(_written),
        "files": available(),
    }


def resolve_day(stream, day=None):
    """The requested day, or the newest one on disk when none is given — so the
    button works both mid-session and the morning after."""
    days = [r["day"] for r in available().get(stream, [])]
    if day:
        return day if day in days else None
    return days[0] if days else None


def path_for(stream, day):
    return os.path.join(TICK_DIR, file_name(stream, day))


def iter_csv(stream, day, chunk=1 << 16):
    """Stream one day's ticks back as plain CSV bytes, decompressed in chunks —
    a full day is gigabytes and must never be materialized in the worker."""
    with gzip.open(path_for(stream, day), "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                return
            yield block


def iter_raw(stream, day, chunk=1 << 16):
    """The gzip file as-is, for callers that would rather transfer ~5x less."""
    with open(path_for(stream, day), "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                return
            yield block
