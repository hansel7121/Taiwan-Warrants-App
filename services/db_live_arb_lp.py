"""CRUD for live_arb_lp_trades (see supabase/schema.sql) — the Live Arb LP
subtab's append-only log of unique static-arb structures. Mirrors
db_live_arb.py's shape, day-scoped and append-only for the same reason: a
row is a record of a moment (a horizon/leg-set going arb-positive), never
re-evaluated once logged. Separate table from live_arb_trades — the row
shape is fundamentally different (multi-leg `legs` JSON, horizon economics,
vs. a two-instrument pair), so a shared table would mean a pile of nullable
columns for one side or the other.
"""
from services import db


def existing_ids_for_date(trade_date):
    """Dedup keys already logged for `trade_date` (a date) — seeds
    live_arb._lp_logged_today on start_lp_scan() so a mid-day restart
    doesn't re-log a structure that already fired earlier that day."""
    r = db._run(
        lambda c: c.table("live_arb_lp_trades")
        .select("id")
        .eq("trade_date", trade_date.isoformat())
        .execute()
    )
    return {row["id"] for row in (r.data or [])}


def insert_trade(row):
    """Plain insert of one newly-logged structure. Caller (live_arb.py) has
    already checked the dedup key isn't logged yet today."""
    db._run(lambda c: c.table("live_arb_lp_trades").insert(row).execute())


def list_trades_for_date(trade_date):
    r = db._run(
        lambda c: c.table("live_arb_lp_trades")
        .select("*")
        .eq("trade_date", trade_date.isoformat())
        .order("detected_at", desc=True)
        .execute()
    )
    return r.data or []
