"""CRUD for live_warrant_tracked (see supabase/schema.sql) — the Live Warrant
tab's shared tracked-code list. Plain reads/writes, no batch/pointer
semantics, mirrors db_products.py. Callers (app.py routes, the scheduler)
decide *which* codes to add/remove by calling logic/live_warrant_logic.py
first; this module only persists whatever they pass in.
"""
from services import db


def list_tracked():
    """Every tracked row, in add order."""
    r = db._run(lambda c: c.table("live_warrant_tracked")
                .select("code, name, source, underlying, created_at")
                .order("created_at").execute())
    return r.data or []


def upsert_tracked(code, name, source, underlying=None):
    """Add one code, or refresh its cached name if already tracked."""
    db._run(lambda c: c.table("live_warrant_tracked").upsert({
        "code": code, "name": name, "source": source, "underlying": underlying,
    }).execute())


def upsert_tracked_many(rows):
    """Bulk upsert, for a scan that adds a whole chain at once — a no-op on an
    empty list. One round-trip instead of one per code, which is what keeps a
    full-chain scan inside the request timeout."""
    if not rows:
        return
    db._run(lambda c: c.table("live_warrant_tracked").upsert(rows).execute())


def remove_tracked(code):
    db._run(lambda c: c.table("live_warrant_tracked").delete().eq("code", code).execute())


def remove_tracked_many(codes):
    """Bulk delete, for a scan's replace step — a no-op on an empty list."""
    if not codes:
        return
    db._run(lambda c: c.table("live_warrant_tracked").delete().in_("code", codes).execute())
