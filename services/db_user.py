"""Per-user watchlist: the scanner rows a user has starred (static/js/watchlist.js).

Every function takes user_id as its
first argument and every query filters on it — the service-role key bypasses
RLS, so that filter is what actually isolates users. Never expose a route here
that takes a user_id from the request body; it must come from g.user.

Mirrors db_products.py: no internal try/except, callers decide how to handle
failures.
"""
from services import db

_WATCH_COLS = "id, kind, code, underlying_code, label, meta, created_at"


# ── Watchlist ────────────────────────────────────────────────────────

def list_watchlist(user_id):
    r = db._run(lambda c: c.table("user_watchlist").select(_WATCH_COLS)
                .eq("user_id", user_id).order("created_at", desc=True).execute())
    return r.data or []


def add_watchlist(user_id, kind, code, underlying_code=None, label=None, meta=None):
    """Star an instrument. Upsert on (user_id, kind, code) so re-starring refreshes meta."""
    row = {"user_id": user_id, "kind": kind, "code": code,
           "underlying_code": underlying_code, "label": label, "meta": meta or {}}
    db._run(lambda c: c.table("user_watchlist")
            .upsert(row, on_conflict="user_id,kind,code").execute())


def remove_watchlist(user_id, kind, code):
    db._run(lambda c: c.table("user_watchlist").delete()
            .eq("user_id", user_id).eq("kind", kind).eq("code", code).execute())
