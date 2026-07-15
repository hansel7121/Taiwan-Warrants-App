"""Request-correlated log lines for the Render log stream.

gunicorn serves 8 threads, so concurrent requests interleave in the log. Every
line carries the originating request's short id ([a1b2c3]) so one request's
story can be read end to end. The data modules are called from BOTH request
threads and the scheduler's jobs, hence the has_request_context() guard: the
id is simply omitted when there is no request.

Like memlog, every helper swallows its own errors — a logging fault must never
break the work being logged.
"""
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


def log(prefix, msg):
    try:
        rid = _current_id()
        print(f"{prefix}: [{rid}] {msg}" if rid else f"{prefix}: {msg}", flush=True)
    except Exception:
        pass


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
