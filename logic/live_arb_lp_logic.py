"""Pure decision logic for the Live Arb LP subtab: static-arb LP against live
TSMC websocket quotes. Mirrors logic/static_arb.py's leg-building and
per-horizon scan shape, but reads the live snapshot's plain dicts (from
live_warrant.snapshot_for_underlying / live_options.snapshot_for_underlying,
already shared with the Direct Match subtab) instead of pandas DataFrames,
and calls the Rust-only rust/warrants_core::solve_static_arb_horizon kernel
directly rather than scipy/HiGHS.

No Flask, no Supabase, no Fubon SDK, no datetime.now() -- "today" is always
passed in by the caller (services/live_arb.py), same convention as
logic/live_arb_logic.py.
"""
import math

from logic import iv_engine

# TW single-stock options: fixed 2,000 shares/contract (CLAUDE.md).
OPT_CONTRACT_SIZE = 2000.0

# Taiwan CBC benchmark-ish rate, matching options_logic.R's default order of
# magnitude; static_arb.py defaults to options_logic.R itself, but this
# module has no Flask/DB context to import that from without a cycle risk,
# and discount over a horizon this short barely moves the answer either way.
R = 0.01875

# Same American-put exception as logic/static_arb.py: a long WARRANT put's
# floor uses intrinsic (K-S)+, not the European lower bound, because a
# warrant is American-exercisable and its holder can simply exercise at the
# horizon -- see that module's docstring for the full reasoning (the LP
# would otherwise certify arbs that don't exist). A long OPTION put stays on
# the European bound since it's European and can't be exercised early.
AMERICAN_PUT_INTRINSIC_FLOOR = True


def horizons(option_rows):
    """Every distinct option expiry currently in the live TSMC option book --
    the set of dates a short leg can settle on (shorts are options-only,
    same rule as logic/static_arb.py's `match_static_arb`)."""
    return sorted({r["expiry"] for r in option_rows if r.get("expiry")})


def _build_legs(warrant_rows, option_rows, horizon, today, m=OPT_CONTRACT_SIZE, r=R):
    """Long/short candidate legs at one horizon. Mirrors
    logic/static_arb.py::_build_legs's filtering and eff_strike/disc math.

    NOTE on depth units: `best["ask_size"]`/`best["bid_size"]` come straight
    from the Fubon websocket book. Whether that figure is in board lots (張)
    -- what logic/static_arb.py's own `ask_qty`/`bid_size` columns are -- or
    raw units is unverified for this live path, same open caveat already
    flagged for the Direct Match subtab (services/live_arb.py's sibling
    module). Treated as lots here for consistency with the rest of the app
    until confirmed live.
    """
    longs, shorts = [], []

    for w in warrant_rows:
        if not w.get("type") or w.get("strike") is None or not w.get("exercise_ratio") or not w.get("maturity"):
            continue
        if w["maturity"] < horizon:
            continue
        dte = (w["maturity"] - today).days
        ratio = float(w["exercise_ratio"])
        if ratio <= 0:
            continue
        lot_shares = 1000.0 * ratio
        best = w["best"]
        ask, qty = best.get("ask"), best.get("ask_size")
        if ask is not None and ask > 0 and qty:
            longs.append({
                "kind": "warrant", "code": w["code"], "is_call": w["type"] == "Call",
                "strike": float(w["strike"]), "dte": dte,
                "price_ps": ask / ratio, "lot_shares": lot_shares,
                "depth_shares": float(qty) * lot_shares,
            })

    for o in option_rows:
        if not o.get("type") or o.get("strike") is None or not o.get("expiry"):
            continue
        dte = (o["expiry"] - today).days
        best = o["best"]
        is_call = o["type"] == "Call"

        if o["expiry"] == horizon:
            bid, size = best.get("bid"), best.get("bid_size")
            if bid is not None and bid > 0 and size:
                shorts.append({
                    "kind": "option", "code": o["code"], "is_call": is_call,
                    "strike": float(o["strike"]), "dte": dte,
                    "price_ps": bid, "lot_shares": float(m),
                    "depth_shares": float(size) * float(m),
                })

        if o["expiry"] >= horizon:
            ask, size = best.get("ask"), best.get("ask_size")
            if ask is not None and ask > 0 and size:
                longs.append({
                    "kind": "option", "code": o["code"], "is_call": is_call,
                    "strike": float(o["strike"]), "dte": dte,
                    "price_ps": ask, "lot_shares": float(m),
                    "depth_shares": float(size) * float(m),
                })

    horizon_dte = (horizon - today).days
    for leg in longs:
        tau = max(0.0, (leg["dte"] - horizon_dte) / 365.0)
        disc = math.exp(-r * tau)
        if AMERICAN_PUT_INTRINSIC_FLOOR and leg["kind"] == "warrant" and not leg["is_call"]:
            disc = 1.0
        leg["eff_strike"] = leg["strike"] * disc
    for leg in shorts:
        leg["eff_strike"] = leg["strike"]

    return longs, shorts


def _leg_out(leg, side, lots):
    shares = lots * leg["lot_shares"]
    sign = 1.0 if side == "short" else -1.0
    return {
        "side": side, "kind": leg["kind"], "code": leg["code"],
        "type": "Call" if leg["is_call"] else "Put",
        "strike": round(leg["strike"], 2), "dte": leg["dte"],
        "price_ps": round(leg["price_ps"], 4), "lots": lots,
        "shares": round(shares, 2),
        "cash": round(sign * leg["price_ps"] * shares, 0),
    }


def scan(warrant_rows, option_rows, today, min_edge=0.0):
    """Run the LP for every horizon in the live TSMC option book.

    Raises RuntimeError if the Rust engine isn't available -- there is no
    Python fallback for this kernel (see iv_engine.solve_static_arb_horizon's
    docstring); the caller (services/live_arb.py) is expected to check
    iv_engine.RUST_AVAILABLE up front and surface a clear "Rust engine
    required" state instead of ever reaching this.
    """
    if not iv_engine.RUST_AVAILABLE:
        raise RuntimeError("Live Arb LP requires the Rust engine, which is not available in this process")

    rows = []
    for horizon in horizons(option_rows):
        longs, shorts = _build_legs(warrant_rows, option_rows, horizon, today)
        if not longs or not shorts:
            continue
        result = iv_engine.solve_static_arb_horizon(
            [l["price_ps"] for l in longs], [l["eff_strike"] for l in longs], [l["is_call"] for l in longs],
            [l["lot_shares"] for l in longs], [l["depth_shares"] for l in longs],
            [s["price_ps"] for s in shorts], [s["eff_strike"] for s in shorts], [s["is_call"] for s in shorts],
            [s["lot_shares"] for s in shorts], [s["depth_shares"] for s in shorts],
            min_edge,
        )
        if result is None:
            continue
        (long_idx, long_lots, short_idx, short_lots,
         net_credit, min_payoff, guaranteed_profit, worst_spot, gross_debit) = result

        legs_out = [_leg_out(longs[i], "long", lots) for i, lots in zip(long_idx, long_lots)]
        legs_out += [_leg_out(shorts[i], "short", lots) for i, lots in zip(short_idx, short_lots)]
        return_pct = round(guaranteed_profit / gross_debit * 100, 2) if gross_debit > 0 else None

        rows.append({
            "horizon_dte": (horizon - today).days,
            "n_long": len(long_idx), "n_short": len(short_idx),
            "legs": legs_out,
            "net_credit": net_credit, "min_payoff": min_payoff,
            "guaranteed_profit": guaranteed_profit, "worst_spot": worst_spot,
            "gross_debit": gross_debit, "return_pct": return_pct,
        })
    return rows


def dedup_key(row, trade_date):
    """Deterministic id for a (horizon, leg-set, day) -- logging the same
    structure again the same day is a no-op. Generalizes
    logic/live_arb_logic.py's pair-key to a multi-leg tuple."""
    codes = sorted(l["code"] for l in row["legs"])
    return f"{row['horizon_dte']}:{'|'.join(codes)}:{trade_date.isoformat()}"
