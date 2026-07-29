"""CRUD for arb_suggestions (see supabase/schema.sql) — automated Direct-Arb
scanner output. Plain reads/writes, no batch/pointer semantics, mirroring
db_products.py: no internal try/except, callers decide how to handle failures.
"""
from services import db


def existing_ids(ids):
    """Return the subset of `ids` already present in arb_suggestions, as a set.

    Used by the append-only scanner to skip re-inserting a trade it has already
    logged (so the stored row's frozen prices + first_seen_at are preserved)."""
    if not ids:
        return set()
    r = db._run(
        lambda c: c.table("arb_suggestions")
        .select("id")
        .in_("id", list(ids))
        .execute()
    )
    return {row["id"] for row in (r.data or [])}


def insert_suggestions(rows):
    """Plain insert (NOT upsert) of brand-new suggestions — the append-only log.

    Caller has already filtered out ids that exist (see existing_ids), so this
    never overwrites a logged row. first_seen_at is stamped here as the append
    minute; status='active' since rows only leave via manual Remove / Clear."""
    if not rows:
        return
    now = db._now()
    payload = [{**row, "status": "active", "first_seen_at": now, "last_seen_at": now}
               for row in rows]
    db._run(lambda c: c.table("arb_suggestions").insert(payload).execute())


def list_active_suggestions():
    # Newest-appended first: the tab reads as a running log ordered by when each
    # trade was captured (first_seen_at). All rows are 'active' (append-only; the
    # scanner never marks anything stale), so the status filter is a no-op guard
    # that still excludes anything a future flow might soft-delete.
    r = db._run(
        lambda c: c.table("arb_suggestions")
        .select("*")
        .eq("status", "active")
        .order("first_seen_at", desc=True)
        .execute()
    )
    return r.data or []


def delete_suggestion(id):
    db._run(lambda c: c.table("arb_suggestions").delete().eq("id", id).execute())


def clear_active_suggestions():
    """Delete every currently-active suggestion (the rows the tab shows). The
    scanner re-populates still-live arbs on its next run, so this only clears
    the current view — a fresh opportunity reappears within one scan window."""
    db._run(lambda c: c.table("arb_suggestions").delete().eq("status", "active").execute())
