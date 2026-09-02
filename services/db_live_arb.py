"""CRUD for live_arb_trades (see supabase/schema.sql) — the Live Arb tab's
append-only log of unique real-time Direct Match hits. Mirrors
db_suggestions.py's shape (existing ids / insert / list), but day-scoped
instead of status-scoped: a Live Arb entry never goes stale or gets
re-evaluated — it's a record of a moment (a pair going arb-positive), not a
still-open position to track.
"""
from services import db


def existing_ids_for_date(trade_date):
    """Dedup keys already logged for `trade_date` (a date) — seeds
    live_arb._logged_today on start_scan() so a mid-day restart doesn't
    re-log a pair that already fired earlier that day."""
    r = db._run(
        lambda c: c.table("live_arb_trades")
        .select("id")
        .eq("trade_date", trade_date.isoformat())
        .execute()
    )
    return {row["id"] for row in (r.data or [])}


def insert_trade(row):
    """Plain insert of one newly-logged trade. Caller (live_arb.py) has
    already checked the dedup key isn't logged yet today."""
    db._run(lambda c: c.table("live_arb_trades").insert(row).execute())


def list_trades_for_date(trade_date):
    r = db._run(
        lambda c: c.table("live_arb_trades")
        .select("*")
        .eq("trade_date", trade_date.isoformat())
        .order("detected_at", desc=True)
        .execute()
    )
    return r.data or []
