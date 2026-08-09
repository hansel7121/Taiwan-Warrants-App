"""Per-user dashboard data: watchlist, alerts, and multi-leg positions.

Backs the user-mode Dashboard tab (static/js/dashboard.js) the same way
store.py backs the admin Portfolio tab. Every function takes user_id as its
first argument and every query filters on it — the service-role key bypasses
RLS, so that filter is what actually isolates users. Never expose a route here
that takes a user_id from the request body; it must come from g.user.

Mirrors db_products.py: no internal try/except, callers decide how to handle
failures.
"""
from services import db

_WATCH_COLS = "id, kind, code, underlying_code, label, meta, created_at"
_ALERT_COLS = ("id, kind, code, underlying_code, metric, direction, threshold, "
               "note, active, last_triggered_at, last_value, created_at")
_LEG_COLS = ("id, position_id, kind, code, label, direction, quantity, entry_price, "
             "option_type, strike, days_to_expiry, exercise_ratio, contract_size, iv, meta")


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


# ── Alerts ───────────────────────────────────────────────────────────

def list_alerts(user_id):
    r = db._run(lambda c: c.table("user_alerts").select(_ALERT_COLS)
                .eq("user_id", user_id).order("created_at", desc=True).execute())
    return r.data or []


def add_alert(user_id, kind, code, metric, direction, threshold,
              underlying_code=None, note=None):
    row = {"user_id": user_id, "kind": kind, "code": code,
           "underlying_code": underlying_code, "metric": metric,
           "direction": direction, "threshold": threshold, "note": note}
    r = db._run(lambda c: c.table("user_alerts").insert(row).execute())
    return (r.data or [{}])[0]


def remove_alert(user_id, alert_id):
    db._run(lambda c: c.table("user_alerts").delete()
            .eq("user_id", user_id).eq("id", alert_id).execute())


def record_trigger(user_id, alert_id, value, fired_at):
    """Persist a client-side evaluation so a fire survives the page reload."""
    db._run(lambda c: c.table("user_alerts")
            .update({"last_triggered_at": fired_at, "last_value": value})
            .eq("user_id", user_id).eq("id", alert_id).execute())


# ── Positions ────────────────────────────────────────────────────────

def list_positions(user_id):
    """Open + closed positions with their legs attached, newest first."""
    r = db._run(lambda c: c.table("user_positions")
                .select("id, name, underlying_code, note, opened_at, closed_at")
                .eq("user_id", user_id).order("opened_at", desc=True).execute())
    positions = r.data or []
    if not positions:
        return []
    ids = [p["id"] for p in positions]
    lr = db._run(lambda c: c.table("user_position_legs").select(_LEG_COLS)
                 .eq("user_id", user_id).in_("position_id", ids).execute())
    by_position = {}
    for leg in (lr.data or []):
        by_position.setdefault(leg["position_id"], []).append(leg)
    for p in positions:
        p["legs"] = by_position.get(p["id"], [])
    return positions


def add_position(user_id, legs, name=None, underlying_code=None, note=None):
    """Insert a position and its legs. Legs are pre-validated by the caller.

    supabase-py has no transactions: if the leg insert fails the empty parent is
    deleted so a legless position never shows up on the dashboard.
    """
    r = db._run(lambda c: c.table("user_positions").insert({
        "user_id": user_id, "name": name,
        "underlying_code": underlying_code, "note": note,
    }).execute())
    position = (r.data or [{}])[0]
    position_id = position.get("id")
    rows = [{**leg, "position_id": position_id, "user_id": user_id} for leg in legs]
    try:
        db._run(lambda c: c.table("user_position_legs").insert(rows).execute())
    except Exception:
        db._run(lambda c: c.table("user_positions").delete()
                .eq("user_id", user_id).eq("id", position_id).execute())
        raise
    position["legs"] = rows
    return position


def remove_position(user_id, position_id):
    """Delete a position; legs cascade on the foreign key."""
    db._run(lambda c: c.table("user_positions").delete()
            .eq("user_id", user_id).eq("id", position_id).execute())


def close_position(user_id, position_id, closed_at):
    db._run(lambda c: c.table("user_positions")
            .update({"closed_at": closed_at, "updated_at": closed_at})
            .eq("user_id", user_id).eq("id", position_id).execute())
