"""Thin persistence seam: Supabase is authoritative, an APP_ENV=local instance also mirrors to portfolio.json for backup/degraded-read; used by get_portfolio/save_portfolio in place of calling db.py directly."""
import json
import os

from services import db

_MIRROR_NAME = "portfolio.json"


def _mirror_path():
    """Path to the plain-array portfolio.json mirror next to this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _MIRROR_NAME)


def _should_mirror():
    """True only when APP_ENV=local; a positive check so a missing/misconfigured APP_ENV never mirrors."""
    return os.environ.get("APP_ENV") == "local"


def _write_mirror(entries):
    with open(_mirror_path(), "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def get_portfolio(user_id):
    """Live portfolio for this user, Supabase-authoritative with a mirror fallback on read failure."""
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
    """Persist to Supabase (sync-safe diff + tombstone), mirroring locally on both success and failure."""
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
