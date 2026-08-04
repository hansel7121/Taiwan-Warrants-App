"""Writes for arb_signals (see supabase/schema.sql) — the live worker's output.

Mirrors db_suggestions.py's shape (thin wrappers over db._run, no internal
try/except) but not its lifecycle: per docs/adr/0007 a signal is an event tied
to the instant of the Tick that produced it, so this is an append-only log with
no status flip and no deterministic id to upsert on. Re-finding the same
warrant/option pair on the next Tick appends a second row on purpose — the two
observations are different facts.
"""
from services import db


TABLE = "arb_signals"

# Matcher output is wide (every leg price, IV, sizing field) and its columns
# differ per strategy. The whole row is kept verbatim in `legs`, exactly as
# arb_suggestions does; only the fields worth filtering or displaying a list on
# are promoted to columns.
def to_signal_row(match, strategy=None, tick_ts=None, tick_broker=None):
    """One matcher row from arb_logic.check_tick -> one arb_signals record."""
    return {
        "strategy": strategy or match.get("strategy") or "unknown",
        "underlying_code": _text(match.get("underlying_code")),
        "warrant_code": _text(match.get("warrant_code")),
        "option_contract": _text(match.get("option_contract")),
        "legs": match,
        "price_diff": match.get("price_diff"),
        "price_diff_pct": match.get("price_diff_pct"),
        "tick_ts": _iso(tick_ts),
        "tick_broker": tick_broker,
    }


def insert_signals(rows):
    """Append signals, stamping the write time. Returns the number written."""
    if not rows:
        return 0
    now = db._now()
    payload = [{**row, "detected_at": row.get("detected_at") or now} for row in rows]
    db._run(lambda c: c.table(TABLE).insert(payload).execute())
    return len(payload)


def list_recent_signals(limit=200):
    """Newest signals first — the live feed reads as a running log."""
    r = db._run(
        lambda c: c.table(TABLE)
        .select("*")
        .order("detected_at", desc=True)
        .limit(limit)
        .execute()
    )
    return r.data or []


def delete_signals_before(cutoff_iso):
    """Age out the log. Nothing else ever removes a signal: without a status to
    flip, time is the only retirement mechanism the append-only shape allows."""
    db._run(lambda c: c.table(TABLE).delete().lt("detected_at", cutoff_iso).execute())


def _text(value):
    return None if value is None else str(value)


def _iso(ts):
    if ts is None:
        return None
    isoformat = getattr(ts, "isoformat", None)
    return isoformat() if isoformat else str(ts)
