"""Pure decision logic for the Live Warrant tab (issue #69).

No Flask context, no Supabase, no Fubon SDK — just the connection-pool
assignment, capacity check, scan-vs-manual replace rule, and ladder-payload
shaping that `services/db_live_warrant.py` and the Fubon websocket glue call
into; those impure layers are exercised manually, not by these tests.
"""

# Real measured caps (see scripts/fubon_quote_viewer.py's own docstring):
# 300 subscriptions/connection, 7 connections/account.
MAX_SUBS_PER_CONN = 300
MAX_CONNECTIONS = 7
MAX_TOTAL_SUBS = MAX_SUBS_PER_CONN * MAX_CONNECTIONS
LEVELS = 5


class CapacityExceededError(RuntimeError):
    """Raised when an add/scan would push total subscriptions past the account cap."""


def assign_slot(conn_counts, max_per_conn=MAX_SUBS_PER_CONN, max_connections=MAX_CONNECTIONS):
    """First-fit index for the next subscription, opening a new connection only when every existing one is full."""
    for i, count in enumerate(conn_counts):
        if count < max_per_conn:
            return i
    if len(conn_counts) < max_connections:
        return len(conn_counts)
    raise CapacityExceededError(
        f"all {max_connections} connections are full ({max_per_conn} subscriptions each)")


def check_capacity(current_total, net_change, max_total=MAX_TOTAL_SUBS):
    """Reject a change that would push total subscriptions past the account-wide cap."""
    if current_total + net_change > max_total:
        raise CapacityExceededError(
            f"adding {net_change} subscription(s) would exceed the {max_total}-subscription "
            f"cap (currently {current_total})")


def scan_replace(existing, underlying, new_codes):
    """Codes to add/remove for a liquidity-scan replace: only this underlying's own scan rows are ever removed."""
    existing_codes = {row["code"] for row in existing}
    to_add = [c for c in new_codes if c not in existing_codes]

    new_set = set(new_codes)
    to_remove = [
        row["code"] for row in existing
        if row["source"] == "scan" and row["underlying"] == underlying and row["code"] not in new_set
    ]
    return to_add, to_remove


def plan_scan_replace(existing, underlying, new_codes, current_total, max_total=MAX_TOTAL_SUBS):
    """scan_replace plus the capacity gate, so a scan is rejected outright rather than partially applied."""
    to_add, to_remove = scan_replace(existing, underlying, new_codes)
    check_capacity(current_total, len(to_add) - len(to_remove), max_total=max_total)
    return to_add, to_remove


def plan_manual_add(existing_codes, code, current_total, max_total=MAX_TOTAL_SUBS):
    """Whether a manual add is a no-op (already tracked) or needs a capacity check first."""
    if code in existing_codes:
        return False
    check_capacity(current_total, 1, max_total=max_total)
    return True


def ladder_rows(bids, asks, levels=LEVELS):
    """Pad raw bid/ask lists to a fixed number of levels, dropping in `None`s for missing depth."""
    rows = []
    for i in range(levels):
        bid = bids[i] if i < len(bids) else None
        ask = asks[i] if i < len(asks) else None
        rows.append({
            "level": i + 1,
            "bid_size": bid["size"] if bid else None,
            "bid": bid["price"] if bid else None,
            "ask": ask["price"] if ask else None,
            "ask_size": ask["size"] if ask else None,
        })
    return rows
