"""CRUD for live_warrant_tracked (see supabase/schema.sql) — the Live Warrant
tab's shared tracked-code list. Plain reads/writes, no batch/pointer
semantics, mirrors db_products.py. Callers (app.py routes, the scheduler)
decide *which* codes to add/remove by calling logic/live_warrant_logic.py
first; this module only persists whatever they pass in.
"""
from datetime import datetime, timezone

from services import db


def list_tracked():
    """Every tracked row, in add order — including any persisted contract
    terms (migration 024), so services/live_warrant.py::start_session() can
    skip re-fetching a code whose terms were already looked up before a
    previous restart."""
    r = db._run(lambda c: c.table("live_warrant_tracked")
                .select("code, name, source, underlying, created_at, "
                        "strike, exercise_ratio, maturity, terms_fetched_at")
                .order("created_at").execute())
    return r.data or []


def upsert_tracked(code, name, source, underlying=None):
    """Add one code, or refresh its cached name if already tracked."""
    db._run(lambda c: c.table("live_warrant_tracked").upsert({
        "code": code, "name": name, "source": source, "underlying": underlying,
    }).execute())


def update_name(code, name):
    """Backfill one row's cached name without touching source/underlying.

    A code is persisted before it is subscribed, so its row is first written
    with the code itself as a placeholder name; this is how the real name lands
    once the REST quote that was throttled during the scan finally succeeds.
    """
    db._run(lambda c: c.table("live_warrant_tracked")
            .update({"name": name}).eq("code", code).execute())


def update_terms(code, strike, exercise_ratio, maturity):
    """Persist one code's contract terms once fetched (see
    services/live_warrant.py::_fetch_terms) — strike/ratio/maturity never
    change for a warrant's life, so this is what lets a future restart load
    them back from here instead of spending a REST round trip re-fetching
    every tracked code. `terms_fetched_at` records that the lookup happened
    at all, even when Fugle's payload left one of the value columns null."""
    db._run(lambda c: c.table("live_warrant_tracked").update({
        "strike": strike,
        "exercise_ratio": exercise_ratio,
        "maturity": maturity.isoformat() if maturity else None,
        "terms_fetched_at": datetime.now(timezone.utc).isoformat(),
    }).eq("code", code).execute())


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
