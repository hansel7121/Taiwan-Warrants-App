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

# How much of an underlying's tracked scan set a whole-chain rescan is allowed to
# delete before it is treated as a bad catalog response rather than a real
# delisting. Warrants do expire in batches, so this is deliberately loose; it
# only has to catch the case where `intraday.tickers` answered with a fraction
# of the chain.
MAX_SCAN_SHRINK = 0.20


class CapacityExceededError(RuntimeError):
    """Raised when an add/scan would push total subscriptions past the account cap."""


class ChainShrinkError(RuntimeError):
    """Raised when a scan's resolved chain is so much smaller than what is already
    tracked for that underlying that a truncated catalog response is the likelier
    explanation — the replace step would delete the difference."""


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


def scan_codes(codes, volumes, top_n):
    """The codes a liquidity scan should subscribe, ranked by traded volume.

    ``top_n`` of 0 (or None) means EVERY warrant on the underlying — the whole
    chain, not a slice. That is what makes a full-chain stress subscribe
    expressible; the account-wide cap in `check_capacity` is still the only
    thing that bounds it. Falls back to listing order when MIS returned no
    volume at all (pre-open, or the endpoint down), so a scan never silently
    subscribes nothing.
    """
    chain = set(codes)
    # MIS is keyed independently of our chain lookup; anything it answers for
    # that is not actually on this underlying must not be subscribed.
    ranked = sorted(((c, v) for c, v in volumes.items() if c in chain),
                    key=lambda kv: kv[1], reverse=True)
    has_volume = bool(ranked and ranked[0][1])

    if not top_n:
        ordered = [c for c, _v in ranked] if has_volume else list(codes)
        # Ranking only covers codes MIS answered for; append the rest so "all"
        # really is all.
        seen = set(ordered)
        return ordered + [c for c in codes if c not in seen]

    if not has_volume:
        return list(codes[:top_n])
    picked = [c for c, _v in ranked[:top_n]]
    # Top up from listing order when MIS answered for fewer than top_n codes: a
    # throttled or failed volume batch has to cost the ranking its confidence,
    # not cost the scan its size. Without this a half-failed MIS call silently
    # subscribes fewer warrants than asked for.
    if len(picked) < top_n:
        seen = set(picked)
        picked += [c for c in codes if c not in seen][:top_n - len(picked)]
    return picked


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


def scan_shrink_ratio(existing, underlying, new_codes):
    """Fraction of this underlying's tracked scan rows the new chain would drop."""
    tracked = {row["code"] for row in existing
               if row.get("source") == "scan" and row.get("underlying") == underlying}
    if not tracked:
        return 0.0
    return len(tracked - set(new_codes)) / len(tracked)


def guard_chain_shrink(existing, underlying, new_codes, top_n, catalog_complete=True,
                       max_shrink=MAX_SCAN_SHRINK, force=False):
    """Refuse a replace that would delete too much of what is already tracked.

    A truncated `intraday.tickers` response looks exactly like a chain that
    shrank overnight, and `scan_replace` deletes the difference — so the
    destructive half of a scan is gated on the new chain being about as large as
    the old one. A ranked top-N scan is *meant* to drop codes that fell out of
    the ranking, so it is only gated when the catalog itself came back
    incomplete; in that case no shrink at all is allowed, because the codes that
    "fell out" may simply be the ones the catalog failed to list.

    Returns the shrink ratio when it lets the scan through, so the caller can
    report it.
    """
    if force:
        return 0.0
    ratio = scan_shrink_ratio(existing, underlying, new_codes)
    if not ratio:
        return 0.0
    if top_n and catalog_complete:
        return ratio
    limit = max_shrink if catalog_complete else 0.0
    if ratio > limit:
        reason = ("" if catalog_complete
                  else " and the warrant catalog came back incomplete")
        raise ChainShrinkError(
            f"scan of {underlying} would drop {ratio:.0%} of the codes already tracked "
            f"for it{reason} — nothing was changed. Re-run with force to apply anyway.")
    return ratio


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


def best_level(bids, asks):
    """Best (level-1) bid/ask price+size only — the collapsed display, replacing
    ladder_rows for a table that no longer shows the full 5-level depth."""
    bid = bids[0] if bids else None
    ask = asks[0] if asks else None
    return {
        "bid": bid["price"] if bid else None,
        "bid_size": bid["size"] if bid else None,
        "ask": ask["price"] if ask else None,
        "ask_size": ask["size"] if ask else None,
    }


def _best(levels):
    return levels[0] if levels else None


def best_level_changed(old_book, new_bids, new_asks):
    """Whether the top of book moved — the dirty gate for a tick-driven recompute.

    True on the first tick (`old_book is None`) or when the best bid/ask price
    or size differs from before. A level-2..5 requote that leaves the best
    level untouched must NOT dirty the code: every displayed/derived column
    depends only on the best level, so recomputing for a deep-book-only change
    would waste exactly the compute this design exists to avoid.
    """
    if old_book is None:
        return True
    old_bid = _best(old_book.get("bids") or [])
    old_ask = _best(old_book.get("asks") or [])
    new_bid = _best(new_bids)
    new_ask = _best(new_asks)
    return old_bid != new_bid or old_ask != new_ask


def parse_warrant_type(name):
    """"Call"/"Put"/None from the standard 購(call)/售(put) character in a
    warrant's Chinese name. Fubon's contract-terms payload carries no explicit
    call/put flag (confirmed against a live probe), so this is the only
    self-contained source available without joining a second data feed."""
    if not name:
        return None
    if "購" in name:
        return "Call"
    if "售" in name:
        return "Put"
    return None
