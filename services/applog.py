"""Request-correlated log lines for the Render log stream.

gunicorn serves 8 threads, so concurrent requests interleave in the log. Every
line carries the originating request's short id ([a1b2c3]) so one request's
story can be read end to end. The data modules are called from BOTH request
threads and the scheduler's jobs, hence the has_request_context() guard: the
id is simply omitted when there is no request.

Like memlog, every helper swallows its own errors — a logging fault must never
break the work being logged.
"""
import threading
import time
import uuid

from flask import g, has_request_context


def new_id():
    return uuid.uuid4().hex[:6]


def _current_id():
    try:
        if has_request_context():
            return getattr(g, "log_id", None)
    except Exception:
        pass
    return None


def log(prefix, msg, level="INFO"):
    """Emit one log line. `level` ("INFO"/"WARN"/"ERROR") is prefixed so the
    Render log stream can be grepped by severity — a genuine fetch failure has
    to be findable among the normal per-code chatter."""
    try:
        rid = _current_id()
        head = f"[{level}] {prefix}"
        print(f"{head}: [{rid}] {msg}" if rid else f"{head}: {msg}", flush=True)
    except Exception:
        pass


_once_lock = threading.Lock()
_once_seen: dict = {}  # key -> epoch of the last line logged under it
ONCE_TTL = 300


def log_once(prefix, msg, key, level="INFO"):
    """log(), but at most once per request under `key`.

    For facts about process-wide state (which universe is in effect) that hold
    for every call in a request: the hot paths ask repeatedly, and the same
    line thirty times is noise, not information. Off the request path there is
    no request to scope to, so those callers are rate-limited to ONCE_TTL.
    """
    try:
        if has_request_context():
            seen = getattr(g, "log_once_seen", None)
            if seen is None:
                seen = set()
                g.log_once_seen = seen
            if key in seen:
                return
            seen.add(key)
        else:
            now = time.time()
            with _once_lock:
                if now - _once_seen.get(key, 0) < ONCE_TTL:
                    return
                _once_seen[key] = now
    except Exception:
        pass  # fail open: a broken guard must not swallow the line
    log(prefix, msg, level)


def redact(secret, keep=6):
    """Truncate a credential so it is identifiable across rotations but unusable."""
    try:
        s = str(secret)
        return f"{s[:keep]}..." if len(s) > keep else "..."
    except Exception:
        return "..."


def set_rows(n):
    """Attach a row count to this request's completion line."""
    try:
        if has_request_context():
            g.log_rows = n
    except Exception:
        pass


def add_stage(name, seconds):
    """Attach a named stage duration to this request's completion line.

    Lets a route report where its time actually went (`enc=3.2ms` for response
    encoding, say) without a second log line to correlate. Off the request path
    it is a no-op, like the rest of this module.
    """
    try:
        if has_request_context():
            stages = getattr(g, "log_stages", None)
            if stages is None:
                stages = {}
                g.log_stages = stages
            stages[name] = stages.get(name, 0.0) + float(seconds)
    except Exception:
        pass
