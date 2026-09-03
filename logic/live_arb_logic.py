"""Pure decision logic for the Live Arb tab: TSMC-only Direct Match against
live websocket quotes (services/live_warrant.py + services/live_options.py's
in-memory books) instead of the batch CMoney/TAIFEX snapshot arb_logic.py
normally matches against.

No Flask context, no Supabase, no Fubon SDK, no `datetime.now()` — "today"
is always passed in by the caller so this stays deterministic and testable.
services/live_arb.py is the impure glue that reads the two live caches, calls
`scan()` on a background timer, and persists logged hits.

Reuses the same Rust-accelerated `direct_pairs` kernel arb_logic.py's Direct
Match uses (see logic/iv_engine.py) directly, skipping the IV-batch solve and
DataFrame frame-build that path does for display purposes only — the match
decision itself needs nothing but bid/ask/strike/dte/ratio/type (see
arb_logic.py's own comment: "No time-value cap / IV solve here: a positive
price arb only needs warrant ask + option bid"), and those are exactly what
a websocket tick carries.
"""
import math

from logic.iv_engine import direct_pairs

# TW single-stock options: fixed 2,000 shares/contract (CLAUDE.md).
OPT_CONTRACT_SIZE = 2000

# Same default as the batch Direct Match route (app.py's /match_warrant_tw_option).
DEFAULT_MAX_DTE_DIFF = 5


def _type_codes(warrant_rows, option_rows):
    """Stable int code per distinct type string, shared across both legs —
    direct_pairs matches by type code equality, not by string."""
    types = sorted({r["type"] for r in warrant_rows if r["type"]} |
                   {r["type"] for r in option_rows if r["type"]})
    return {t: i for i, t in enumerate(types)}


def build_direct_arrays(warrant_rows, option_rows, today):
    """Shape raw snapshot rows (live_warrant.snapshot_for_underlying /
    live_options.snapshot_for_underlying) into resolved per-leg dicts.

    A row with unresolved type/strike/ratio/maturity (still pending its
    first REST seed or terms fetch) or an already-expired maturity is
    dropped rather than passed through as NaN/None — direct_pairs assumes
    fully-resolved numeric inputs, same contract arb_logic.py's frame-build
    already guarantees for the batch path.
    """
    w = []
    for r in warrant_rows:
        if not r["type"] or r["strike"] is None or r["exercise_ratio"] is None or not r.get("maturity"):
            continue
        dte = (r["maturity"] - today).days
        if dte <= 0:
            continue
        best = r["best"]
        w.append({
            "code": r["code"], "name": r["name"], "type": r["type"],
            "strike": float(r["strike"]), "dte": dte, "ratio": float(r["exercise_ratio"]),
            "ask": float(best["ask"]) if best.get("ask") is not None else float("nan"),
            "bid": float(best["bid"]) if best.get("bid") is not None else float("nan"),
            "ask_size": best.get("ask_size"), "bid_size": best.get("bid_size"),
        })

    o = []
    for r in option_rows:
        if not r["type"] or r["strike"] is None or not r.get("expiry"):
            continue
        dte = (r["expiry"] - today).days
        if dte <= 0:
            continue
        best = r["best"]
        bid, ask = best.get("bid"), best.get("ask")
        o.append({
            "code": r["code"], "name": r["name"], "type": r["type"],
            "strike": float(r["strike"]), "dte": dte,
            "bid": float(bid) if bid is not None else float("nan"),
            "ask": float(ask) if ask is not None else float("nan"),
            # No settlement-fallback concept exists on the live book (unlike
            # the batch fetch's is_live flag) — any present price IS the live
            # quote, so "live" here just means "the book has a price at all".
            "bid_live": bid is not None, "ask_live": ask is not None,
            "bid_size": best.get("bid_size"), "ask_size": best.get("ask_size"),
        })
    return w, o


def scan(warrant_rows, option_rows, today, max_dte_diff=DEFAULT_MAX_DTE_DIFF):
    """Run direct_pairs against a live TSMC snapshot; return only the
    executable direction (positive: buy warrant / sell option). Warrants are
    long-only (CLAUDE.md), so the sell-warrant/buy-option direction is never
    actionable and is dropped here rather than surfaced for display.
    """
    w, o = build_direct_arrays(warrant_rows, option_rows, today)
    if not w or not o:
        return []

    codes = _type_codes(w, o)
    hits = direct_pairs(
        [codes[r["type"]] for r in w],
        [r["type"] == "Put" for r in w],
        [r["strike"] for r in w],
        [r["dte"] for r in w],
        [r["ratio"] for r in w],
        [r["ask"] for r in w],
        [r["bid"] for r in w],
        [codes[r["type"]] for r in o],
        [r["strike"] for r in o],
        [r["dte"] for r in o],
        [r["bid"] for r in o],
        [r["ask"] for r in o],
        [r["bid_live"] for r in o],
        [r["ask_live"] for r in o],
        max_dte_diff, False,
    )

    rows = []
    seen = set()
    for (wi, oi, dir_code, price_diff, exec_opt, exec_warrant,
         strike_diff_pct, dte_diff, favorable, max_loss_per_share) in hits:
        if dir_code != 0:  # 0 == positive direction, see arb_logic.py's _match_warrants_to_options
            continue
        # direct_pairs deliberately lets a NaN-priced pair through when a
        # side is missing (its own docstring: "NaN quotes propagate...").
        # The batch arb_logic.py path never hits this in practice because a
        # REST-fetched warrant frame essentially always has some ask value;
        # a live websocket book genuinely can have no ask yet (illiquid /
        # one-sided), so this filter is load-bearing here in a way it isn't
        # upstream — without it, a NaN slips into the JSON response as the
        # bare (invalid-per-spec) token `NaN`, which the browser's
        # JSON.parse rejects outright.
        if not (math.isfinite(price_diff) and math.isfinite(exec_warrant) and math.isfinite(exec_opt)):
            continue
        wr, opt = w[wi], o[oi]
        pair_key = (wr["code"], opt["code"])
        if pair_key in seen:
            continue
        seen.add(pair_key)
        rows.append({
            "warrant_code": wr["code"], "warrant_name": wr["name"],
            "option_code": opt["code"], "option_name": opt["name"],
            "type": wr["type"],
            "warrant_strike": wr["strike"], "opt_strike": round(float(opt["strike"]), 2),
            "warrant_dte": wr["dte"], "opt_dte": opt["dte"], "dte_diff": int(dte_diff),
            "warrant_ask": wr["ask"], "opt_bid": opt["bid"],
            "warrant_ask_size": wr["ask_size"], "opt_bid_size": opt["bid_size"],
            "price_diff": round(float(price_diff), 4),
            "price_diff_pct": round(price_diff / exec_opt * 100, 2) if exec_opt else None,
            "riskless": bool(favorable),
        })
    return rows


def best_per_warrant(rows):
    """Reduce active hits to the single best (max price_diff) option leg per
    warrant — the "pick the best one" rule for what actually gets logged."""
    best = {}
    for r in rows:
        code = r["warrant_code"]
        if code not in best or r["price_diff"] > best[code]["price_diff"]:
            best[code] = r
    return list(best.values())


def dedup_key(row, trade_date):
    """Deterministic id for a (warrant, option, day) — logging this pair
    again the same day is a no-op, mirroring arb_suggestions' id scheme."""
    return f"{row['warrant_code']}:{row['option_code']}:{trade_date.isoformat()}"


def latest_tick(warrant_rows, option_rows):
    """The single most recently ticked row across both live snapshots, by
    `ts` — feeds the "Last received tick" debug line in services/live_arb.py's
    get_data()/get_lp_data(). None when neither side has ticked yet.

    Only `src == "ws"` rows count: a REST-seeded book (see
    services/live_warrant.py::_seed_from_rest and the mirrored
    services/live_options.py version) fills a cell before any tick arrives,
    but it isn't a tick the exchange pushed, and it never bumps the
    tick-seq counters `_combined_seq()` sums — so filtering to "ws" here
    keeps this signal in lockstep with what that counter actually measures.

    Every tracked row is a candidate, not just the ones with fully resolved
    terms `build_direct_arrays` keeps — this is a raw liveness signal, not
    an arb-matching input, and deliberately shows tick flow even when
    nothing is currently matchable.
    """
    candidates = [("warrant", r) for r in warrant_rows if r.get("src") == "ws" and r.get("ts") is not None]
    candidates += [("option", r) for r in option_rows if r.get("src") == "ws" and r.get("ts") is not None]
    if not candidates:
        return None
    kind, row = max(candidates, key=lambda kr: kr[1]["ts"])
    best = row.get("best") or {}
    return {
        "kind": kind, "code": row["code"], "name": row.get("name"),
        "bid": best.get("bid"), "ask": best.get("ask"), "ts": row["ts"],
    }
