"""Thin persistence seam: Supabase is authoritative, local keeps a JSON mirror.

Both the Render (production) and localhost (redundancy) instances call through
here instead of db.py directly. Supabase is always the source of truth. On a
non-Render instance we additionally mirror the portfolio to portfolio.json next
to this module so there is a readable backup and a degraded read-only fallback
when Supabase is unreachable. There is NO offline write buffer and NO merge /
conflict logic — on Render nothing touches disk at all.
"""
import json
import os

from services import db

_MIRROR_NAME = "portfolio.json"


def _mirror_path():
    """Path to the plain-array portfolio.json mirror next to this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _MIRROR_NAME)


def _should_mirror():
    """True only off Render. Render is ephemeral / read-only and never mirrors."""
    return not os.environ.get("RENDER")


def _write_mirror(entries):
    with open(_mirror_path(), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def get_portfolio(user_id):
    """Live portfolio for this user, Supabase-authoritative.

    On success, a non-Render instance refreshes the portfolio.json mirror. If
    Supabase raises, fall back to the mirror (degraded read) when one exists;
    otherwise re-raise.
    """
    try:
        entries = db.get_portfolio(user_id)
    except Exception as e:
        path = _mirror_path()
        if os.path.exists(path):
            print(f"STORE: Supabase read failed ({e}); serving mirror {path}", flush=True)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise
    if _should_mirror():
        _write_mirror(entries)
    return entries


def save_portfolio(user_id, entries):
    """Persist to Supabase (sync-safe diff + tombstone), then mirror off Render.

    On Supabase failure a non-Render instance still writes entries to the mirror
    so the input isn't lost, then re-raises so the caller/frontend can surface
    "not synced". No queue, no retry.
    """
    try:
        db.save_portfolio(user_id, entries)
    except Exception as e:
        if _should_mirror():
            print(f"STORE: Supabase write failed ({e}); persisted mirror only", flush=True)
            _write_mirror(entries)
        raise
    if _should_mirror():
        _write_mirror(entries)
    return True
