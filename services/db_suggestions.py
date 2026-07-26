"""CRUD for arb_suggestions (see supabase/schema.sql) — automated Direct-Arb
scanner output. Plain reads/writes, no batch/pointer semantics, mirroring
db_products.py: no internal try/except, callers decide how to handle failures.
"""
from services import db


def upsert_suggestions(rows):
    """Upsert by id, bumping last_seen_at/status='active' on every touched row."""
    if not rows:
        return
    payload = [{**row, "status": "active", "last_seen_at": db._now()} for row in rows]
    db._run(lambda c: c.table("arb_suggestions").upsert(payload).execute())


def mark_stale(arb_type, touched_ids):
    """Flip status='stale' for any active row of `arb_type` not in touched_ids."""
    q = (
        lambda c: c.table("arb_suggestions")
        .update({"status": "stale"})
        .eq("arb_type", arb_type)
        .eq("status", "active")
        .not_.in_("id", list(touched_ids) or [""])
        .execute()
    )
    db._run(q)


def list_active_suggestions():
    r = db._run(
        lambda c: c.table("arb_suggestions")
        .select("*")
        .eq("status", "active")
        .order("price_diff_pct", desc=True)
        .execute()
    )
    return r.data or []


def delete_suggestion(id):
    db._run(lambda c: c.table("arb_suggestions").delete().eq("id", id).execute())
