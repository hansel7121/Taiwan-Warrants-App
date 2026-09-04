"""Static-arbitrage LP over a whole Taiwan warrant + option chain.

Where arb_logic hand-codes one structure per model (vertical, put-call parity,
butterfly) with fixed weights, this module solves for the weights instead: one
linear program per (underlying, horizon), maximising the entry credit subject to
the portfolio's payoff being >= 0 at every spot. Within the class of static
buy-and-hold portfolios of quoted instruments sharing one horizon it is
complete — every such arb is found, and an empty result is a proof that none
exists. arb_logic's three models all fall out as weight-restricted special
cases. Drives the Experiment Arb Finder sub-tab via app.py::match_static_arb.
Warrants are BUY-ONLY: a long basket may hold warrants and options, a short
basket holds options only. Shorting a warrant means shorting an American claim
the holder may exercise before the horizon, which the one-period payoff model
cannot bound — arb_logic's "Buy Option / Sell Warrant" direction lives in Direct
Match, not here. Scoped to Taiwan warrants vs Taiwan options.
"""
import numpy as np
import pandas as pd
from scipy.optimize import linprog

from services import applog
from logic import arb_logic
from logic import iv_engine
from logic import options_logic
from logic import warrant_logic

# Long legs are floored at their EUROPEAN lower bound, (S - K*d)+ for calls and
# (K*d - S)+ for puts — EXCEPT long warrant puts, which get the intrinsic floor
# (K - S)+ instead. This flag is that exception.
#
# The put case is the whole reason the bound matters: a European put may trade
# BELOW intrinsic, so flooring one at (K - S)+ would certify arbs that do not
# exist. That applies to a long OPTION put, which is European and cannot be
# exercised before its own expiry, and the flag deliberately does not touch it.
#
# A WARRANT is American-exercisable, so at the horizon its holder can simply
# exercise and take (K - S)+. The European bound is what you may assume only
# when early exercise is unavailable, and for a put it is strictly smaller — so
# applying it to a warrant understates the leg and makes the LP refuse
# structures Direct Match correctly reports. Same-strike pairs with the warrant
# merely longer-dated have no strike cushion at all and were rejected outright;
# see tests/logic/test_lp_captures_direct.py.
#
# Accepted residual: exercise settles against a reference price rather than the
# instantaneous spot, and the short option settles against its own final
# settlement price, so the two legs carry one day of reference-price basis. That
# is far smaller than the discount it replaces (0.93% per 180 days of extra
# warrant life), but it is not zero — this is a bounded-basis claim, not an
# unconditional proof. Calls are unaffected either way: there the discount
# lowers the long strike and only lifts the payoff.
AMERICAN_PUT_INTRINSIC_FLOOR = True

# LP solver noise floor, as a fraction of a LEG'S OWN lot size. Scaled per-leg
# rather than to the largest weight anywhere else in a shared multi-leg
# horizon solve -- a global threshold let a real small edge get read as dust
# next to an unrelated deep-liquidity leg (see the 2026-09 arb-ticket bug:
# a $550 real vertical vanished next to fake legs sized at 10,000 lots).
_LEG_DUST_FRAC = 1e-6

_TOL = 1e-6

# _solve_horizon re-solves the LP from scratch after evicting a leg, so this
# bounds the number of re-solves, not the number of legs in the chain -- each
# round strictly shrinks the candidate pool by at least one leg.
_MAX_REPAIR_ROUNDS = 25


def _int_or(v, default=0):
    """Coerce a possibly-NaN/None quote size to int."""
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _leg_payoff(leg, S):
    """Terminal payoff (shorts) or lower-bound value (longs) at the horizon."""
    k = leg["eff_strike"]
    return max(0.0, S - k) if leg["is_call"] else max(0.0, k - S)


def _leg_slope(leg):
    """Payoff slope as S -> infinity: calls run with spot, puts flatten to zero."""
    return 1.0 if leg["is_call"] else 0.0


def _kink_points(longs, shorts):
    """Every spot where the summed payoff can bend, plus zero."""
    pts = {0.0}
    for leg in list(longs) + list(shorts):
        pts.add(round(float(leg["eff_strike"]), 6))
    return sorted(pts)


def _net_payoff(longs, shorts, xl, xs, S):
    """Portfolio payoff at spot S for the given share weights."""
    return (sum(w * _leg_payoff(l, S) for l, w in zip(longs, xl))
            - sum(w * _leg_payoff(s, S) for s, w in zip(shorts, xs)))


def _discount(leg, T_star, r):
    """Fill in the horizon discount and the strike it moves the leg's floor to."""
    tau = max(0.0, (leg["dte"] - T_star) / 365.0)
    disc = float(np.exp(-r * tau))
    if AMERICAN_PUT_INTRINSIC_FLOOR and leg["kind"] == "warrant" and not leg["is_call"]:
        disc = 1.0
    leg["disc"] = disc
    leg["eff_strike"] = leg["strike"] * disc
    return leg


def _warrant_leg(w, T_star, r):
    """One warrant row's long leg at T_star, or (None, dropped) when it has no
    buyable quote. Also called one row at a time by the Rust scan path, which
    hands back indices and needs the display fields rebuilt for just those."""
    dte = int(w["days_to_expiry"])
    if dte < T_star:
        return None, 0
    ratio = float(w.get("exercise_ratio") or 0)
    if ratio <= 0:
        return None, 0
    ask = float(w["ask"]) if pd.notna(w.get("ask")) else 0.0
    if ask <= 0:
        return None, 0
    qty = _int_or(w.get("ask_qty"))
    if qty <= 0:
        return None, 1
    lot_shares = 1000.0 * ratio   # one 張 = 1,000 units, each delivering `ratio` shares
    return _discount({
        "kind": "warrant", "code": str(w["warrant_code"]),
        "name": str(w["warrant_name"]), "type": w["type"],
        "is_call": w["type"] == "Call", "strike": float(w["strike"]),
        "dte": dte, "ratio": ratio, "lot_shares": lot_shares,
        "quote": round(ask, 4), "price_ps": round(ask / ratio, 6),
        "depth_lots": qty, "depth_shares": qty * lot_shares,
    }, T_star, r), 0


def _option_legs(o, T_star, M, r):
    """One option row's (short, long, dropped) at T_star. A short must expire at
    EXACTLY T_star — one that settled earlier settled against the spot at its own
    expiry, a different random variable a one-period LP cannot represent. A long
    may expire at or beyond T_star and is replaced by its lower bound there."""
    dte = int(o["days_to_expiry"])
    typ = o["type"]
    base = {
        "kind": "option", "code": str(o["contract"]), "name": str(o["contract"]),
        "type": typ, "is_call": typ == "Call", "strike": float(o["strike"]),
        "dte": dte, "ratio": None, "lot_shares": float(M),
    }
    short = long = None
    dropped = 0

    if dte == T_star and bool(o.get("bid_live", False)):
        bid = float(o["bid"]) if pd.notna(o.get("bid")) else 0.0
        size = _int_or(o.get("bid_size"))
        if bid > 0:
            if size <= 0:
                dropped += 1
            else:
                short = {**base, "quote": round(bid, 4), "price_ps": round(bid, 6),
                         "depth_lots": size, "depth_shares": size * float(M),
                         "disc": 1.0, "eff_strike": base["strike"]}

    if dte >= T_star and bool(o.get("ask_live", False)):
        ask = float(o["ask"]) if pd.notna(o.get("ask")) else 0.0
        size = _int_or(o.get("ask_size"))
        if ask > 0:
            if size <= 0:
                dropped += 1
            else:
                long = _discount({**base, "quote": round(ask, 4),
                                  "price_ps": round(ask, 6), "depth_lots": size,
                                  "depth_shares": size * float(M)}, T_star, r)

    return short, long, dropped


def _build_legs(warrant_df, opt_df, T_star, M, r):
    """Normalise both chains to per-underlying-share legs at horizon T_star.

    Warrants enter the long side only; the short side is options exclusively.
    """
    longs, shorts, dropped_no_depth = [], [], 0

    for _, w in warrant_df.iterrows():
        leg, dropped = _warrant_leg(w, T_star, r)
        dropped_no_depth += dropped
        if leg is not None:
            longs.append(leg)

    for _, o in opt_df.iterrows():
        short, long, dropped = _option_legs(o, T_star, M, r)
        dropped_no_depth += dropped
        if short is not None:
            shorts.append(short)
        if long is not None:
            longs.append(long)

    return longs, shorts, dropped_no_depth


def _leg_dust(leg):
    """LP solver noise floor for one leg, scaled to its own lot size."""
    return leg["lot_shares"] * _LEG_DUST_FRAC


def _round_longs(cand_l, z_l):
    """Round each long UP to whole tradable lots.

    Floors are non-negative and non-decreasing in the long weights, so this
    only ever lifts the payoff curve. Returns (keep, xl, forced_out): a long
    whose rounded-up lot size exceeds its own resting depth cannot be bought
    at all and is reported in `forced_out` for the caller to evict from the
    candidate pool and re-solve around, rather than voiding every other leg
    bundled into the same horizon.
    """
    keep, xl, forced_out = [], [], []
    for leg, v in zip(cand_l, z_l):
        if v <= _leg_dust(leg):
            continue
        lots = int(np.ceil(v / leg["lot_shares"] - _TOL))
        if lots <= 0:
            continue
        shares = lots * leg["lot_shares"]
        if shares > leg["depth_shares"] + _TOL:
            forced_out.append(leg)
            continue
        keep.append({**leg, "lots": lots, "shares": shares})
        xl.append(shares)
    return keep, xl, forced_out


def _round_shorts(cand_s, z_s):
    """Round each short DOWN to whole tradable lots -- only ever shrinks what
    is subtracted, so this can never exceed the short's own resting depth."""
    keep, xs = [], []
    for leg, v in zip(cand_s, z_s):
        if v <= _leg_dust(leg):
            continue
        lots = int(np.floor(v / leg["lot_shares"] + _TOL))
        if lots <= 0:
            continue
        shares = lots * leg["lot_shares"]
        keep.append({**leg, "lots": lots, "shares": shares})
        xs.append(shares)
    return keep, xs


def _worst_rounded_leg(cand_l, z_l, cand_s, z_s):
    """The leg whose lot-rounding moved furthest, in NT$, from what the raw LP
    solution actually wanted -- the likeliest reason a structure that was
    profitable in continuous terms turned unprofitable once forced onto whole
    lots. Returns (leg, side) so the caller can evict it from the SAME list
    identity was found in: the same option code can appear as separate long
    and short leg dicts (bought at ask, sold at bid) with identical fields
    whenever ask == bid, so an equality-based lookup could evict the wrong
    copy. (None, None) once every leg already sits on (or near) a lot
    boundary.
    """
    best_leg, best_side, best_cost = None, None, 0.0
    for leg, v in zip(cand_l, z_l):
        if v <= _leg_dust(leg):
            continue
        lots = int(np.ceil(v / leg["lot_shares"] - _TOL))
        shares = lots * leg["lot_shares"]
        cost = abs(shares - v) * leg["price_ps"]
        if cost > best_cost:
            best_leg, best_side, best_cost = leg, "long", cost
    for leg, v in zip(cand_s, z_s):
        if v <= _leg_dust(leg):
            continue
        lots = int(np.floor(v / leg["lot_shares"] + _TOL))
        shares = lots * leg["lot_shares"]
        cost = abs(shares - v) * leg["price_ps"]
        if cost > best_cost:
            best_leg, best_side, best_cost = leg, "short", cost
    return best_leg, best_side


def _leg_out(leg, side):
    """Flatten one solved leg for the API row."""
    sign = 1.0 if side == "short" else -1.0
    return {
        "side": side, "kind": leg["kind"], "code": leg["code"], "name": leg["name"],
        "type": leg["type"], "strike": round(leg["strike"], 2), "dte": leg["dte"],
        "eff_strike": round(leg["eff_strike"], 4), "quote": leg["quote"],
        "price_ps": round(leg["price_ps"], 4), "lots": leg["lots"],
        "lot_label": "張" if leg["kind"] == "warrant" else "口",
        "shares": round(leg["shares"], 2), "depth_lots": leg["depth_lots"],
        "ratio": leg.get("ratio"),
        "cash": round(sign * leg["price_ps"] * leg["shares"], 0),
    }


def _solve_lp(longs, shorts):
    """Solve the continuous relaxation for one leg set. Returns (res, kinks).

    maximise  sum(y_j * bid_j) - sum(x_i * cost_i)          [entry credit]
    s.t.      sum(x_i * floor_i(S)) - sum(y_j * payoff_j(S)) >= 0  at every kink
              same with slopes, for S -> infinity
              0 <= x_i <= depth_i,  0 <= y_j <= depth_j

    Every leg payoff is piecewise-linear with one kink, so the sum bends only at
    {eff_strike}. A piecewise-linear function is >= 0 everywhere iff it is >= 0
    at S=0, at every kink, and its far-right slope is >= 0 — so this finite
    constraint set is EXACTLY equivalent to the infinite condition, not a sample.
    """
    kinks = _kink_points(longs, shorts)
    nL, nS = len(longs), len(shorts)

    # linprog minimises, so the objective is (cost of buys) - (proceeds of sells).
    c = np.concatenate([np.array([l["price_ps"] for l in longs], dtype=float),
                        -np.array([s["price_ps"] for s in shorts], dtype=float)])

    A = np.zeros((len(kinks) + 1, nL + nS), dtype=float)
    for p, S in enumerate(kinks):
        for i, leg in enumerate(longs):
            A[p, i] = -_leg_payoff(leg, S)
        for j, leg in enumerate(shorts):
            A[p, nL + j] = _leg_payoff(leg, S)
    for i, leg in enumerate(longs):
        A[-1, i] = -_leg_slope(leg)
    for j, leg in enumerate(shorts):
        A[-1, nL + j] = _leg_slope(leg)

    bounds = ([(0.0, l["depth_shares"]) for l in longs]
              + [(0.0, s["depth_shares"]) for s in shorts])

    res = linprog(c, A_ub=A, b_ub=np.zeros(len(kinks) + 1), bounds=bounds,
                  method="highs")
    return res, kinks


def _solve_horizon(longs, shorts, T_star, min_edge):
    """Solve one horizon's LP and return a finished row, or None.

    A single shared LP over every warrant/option at this horizon can find a
    structure that is profitable in CONTINUOUS shares but turns unprofitable
    once each leg is forced onto whole tradable lots (options round in
    2,000-share jumps; a leg the raw solution wanted only a fraction of can
    overshoot badly). The old behaviour voided the ENTIRE combined structure
    when that happened, silently hiding every other, unrelated, genuinely
    valid arb bundled into the same horizon. Instead: evict whichever leg's
    rounding cost the most and re-solve the remainder from scratch, so one
    badly-rounding leg (or a handful of quote-tick-noise legs sharing a
    resting-depth-heavy horizon with a real mispricing) no longer buries a
    real arb sitting right next to it.
    """
    cand_l, cand_s = list(longs), list(shorts)

    for _ in range(_MAX_REPAIR_ROUNDS):
        if not cand_l or not cand_s:
            return None

        res, kinks = _solve_lp(cand_l, cand_s)
        if not res.success or res.x is None:
            return None
        if -float(res.fun) <= max(min_edge, _TOL):
            return None

        nL = len(cand_l)
        z = np.asarray(res.x, dtype=float)
        keep_l, xl, forced_out = _round_longs(cand_l, z[:nL])
        if forced_out:
            # Can't buy enough of this long to cover what it was meant to
            # protect -- it never belonged in this structure. Drop it and
            # re-solve the rest, rather than voiding everything around it.
            cand_l = [l for l in cand_l if l not in forced_out]
            continue
        keep_s, xs = _round_shorts(cand_s, z[nL:])
        if not keep_l or not keep_s:
            return None

        # Re-verify from scratch on the integer weights. Shorts only ever
        # shrink under floor-rounding and longs only ever grow under
        # ceil-rounding, so both moves can only LIFT the net payoff relative
        # to the already-nonnegative continuous solution -- a violation here
        # means a dust-dropped long was load-bearing, which has no cheap
        # partial fix, so this candidate set is abandoned outright.
        kinks2 = _kink_points(keep_l, keep_s)
        tail = (sum(w * _leg_slope(l) for l, w in zip(keep_l, xl))
                - sum(w * _leg_slope(s) for s, w in zip(keep_s, xs)))
        if tail < -_TOL:
            return None
        payoffs = [_net_payoff(keep_l, keep_s, xl, xs, S) for S in kinks2]
        min_payoff = min(payoffs)
        if min_payoff < -_TOL:
            return None

        gross_debit = sum(l["price_ps"] * w for l, w in zip(keep_l, xl))
        proceeds = sum(s["price_ps"] * w for s, w in zip(keep_s, xs))
        credit = proceeds - gross_debit
        guaranteed = credit + max(0.0, min_payoff)
        # Credit must be positive: cash in today, never a payout later. Debit-financed
        # structures can also be arbs but tie up capital, so they are out of scope.
        if credit > 0 and guaranteed > min_edge:
            return {
                "underlying_code": None,      # set by the caller
                "underlying_price": None,
                "horizon_dte": int(T_star),
                "n_legs": len(keep_l) + len(keep_s),
                "n_long": len(keep_l), "n_short": len(keep_s),
                "legs": ([_leg_out(l, "long") for l in keep_l]
                         + [_leg_out(s, "short") for s in keep_s]),
                "net_credit": round(credit, 0),
                "min_payoff": round(min_payoff, 0),
                "guaranteed_profit": round(guaranteed, 0),
                "worst_spot": round(kinks2[int(np.argmin(payoffs))], 2),
                "gross_debit": round(gross_debit, 0),
                "return_pct": round(guaranteed / gross_debit * 100, 2) if gross_debit > 0 else None,
                "riskless": True,
                "fillable": True,
            }

        # Profitable in continuous terms but not after lot rounding: evict
        # whichever leg's rounding cost the most (in NT$) relative to what
        # the raw solution wanted, and re-solve the remainder from scratch.
        worst, worst_side = _worst_rounded_leg(cand_l, z[:nL], cand_s, z[nL:])
        if worst is None:
            return None   # every leg already sits on a lot boundary -- genuinely too thin
        if worst_side == "long":
            cand_l = [l for l in cand_l if l is not worst]
        else:
            cand_s = [l for l in cand_s if l is not worst]

    return None


def _num_col(df, name, default, dtype):
    """One frame column as a plain Python list for the Rust kernel."""
    col = df[name] if name in df.columns else pd.Series(default, index=df.index)
    return pd.to_numeric(col, errors="coerce").fillna(default).astype(dtype).tolist()


def _bool_col(df, name):
    col = df[name] if name in df.columns else pd.Series(False, index=df.index)
    return col.fillna(False).astype(bool).tolist()


_WARRANT_LEG_COLS = ("warrant_code", "warrant_name", "type", "strike",
                     "exercise_ratio", "days_to_expiry", "ask", "ask_qty")
_OPTION_LEG_COLS = ("contract", "type", "strike", "days_to_expiry",
                    "bid", "bid_size", "bid_live", "ask", "ask_size", "ask_live")


def _row_reader(df, cols):
    """`(i) -> row dict`, off plain Python lists.

    Rust hands back row indices, and pulling those rows out of the frame is the
    obvious way to rebuild them — but `.iloc[i]` builds a Series per leg and
    `.to_dict("records")` walks every column of every row, either of which costs
    more than the whole solve across a 60-leg structure. One `tolist()` per
    column instead, and a dict comprehension per leg actually used.
    """
    data = {c: df[c].tolist() for c in cols if c in df.columns}
    return lambda i: {c: v[i] for c, v in data.items()}


def _solved_row(warrant_row, opt_row, T_star, M, r, solved):
    """One Rust-solved horizon as the API row `_solve_horizon` would have built.

    Rust returns row indices and lot counts only, so the display fields come
    back from the same `_warrant_leg`/`_option_legs` the Python path uses —
    re-run for the handful of legs in the answer rather than the whole chain.
    """
    (long_kind, long_idx, long_lots, short_idx, short_lots,
     net_credit, min_payoff, guaranteed, worst_spot, gross_debit) = solved

    def sized(leg, lots):
        return {**leg, "lots": int(lots), "shares": lots * leg["lot_shares"]}

    keep_l = []
    for kind, i, lots in zip(long_kind, long_idx, long_lots):
        if kind == 0:
            leg, _ = _warrant_leg(warrant_row(i), T_star, r)
        else:
            _, leg, _ = _option_legs(opt_row(i), T_star, M, r)
        keep_l.append(sized(leg, lots))
    keep_s = [sized(_option_legs(opt_row(j), T_star, M, r)[0], lots)
              for j, lots in zip(short_idx, short_lots)]

    return {
        "underlying_code": None,      # set by the caller
        "underlying_price": None,
        "horizon_dte": int(T_star),
        "n_legs": len(keep_l) + len(keep_s),
        "n_long": len(keep_l), "n_short": len(keep_s),
        "legs": ([_leg_out(l, "long") for l in keep_l]
                 + [_leg_out(s, "short") for s in keep_s]),
        "net_credit": net_credit,
        "min_payoff": min_payoff,
        "guaranteed_profit": guaranteed,
        "worst_spot": worst_spot,
        "gross_debit": gross_debit,
        "return_pct": round(guaranteed / gross_debit * 100, 2) if gross_debit > 0 else None,
        "riskless": True,
        "fillable": True,
    }


def _scan_chain(warrant_df, opt_df, horizons, M, r, min_edge):
    """Every horizon of one underlying, built and solved in one Rust call.

    The Python path below rebuilds every leg in the chain once per horizon,
    which on a 800-warrant book costs as much as all the LPs put together.
    """
    outcomes = iv_engine.scan_static_arb(
        _num_col(warrant_df, "days_to_expiry", 0, "int64"),
        (warrant_df["type"].astype(str) == "Call").tolist(),
        _num_col(warrant_df, "strike", 0.0, "float64"),
        _num_col(warrant_df, "exercise_ratio", 0.0, "float64"),
        _num_col(warrant_df, "ask", 0.0, "float64"),
        _num_col(warrant_df, "ask_qty", 0, "int64"),
        _num_col(opt_df, "days_to_expiry", 0, "int64"),
        (opt_df["type"].astype(str) == "Call").tolist(),
        _num_col(opt_df, "strike", 0.0, "float64"),
        _num_col(opt_df, "bid", 0.0, "float64"),
        _num_col(opt_df, "bid_size", 0, "int64"),
        _bool_col(opt_df, "bid_live"),
        _num_col(opt_df, "ask", 0.0, "float64"),
        _num_col(opt_df, "ask_size", 0, "int64"),
        _bool_col(opt_df, "ask_live"),
        horizons, M, r, min_edge,
    )
    rows, dropped = [], 0
    warrant_row = opt_row = None
    for T_star, drop, solved in outcomes:
        dropped += drop
        if solved is None:
            continue
        if warrant_row is None:   # only worth building once a structure exists
            warrant_row = _row_reader(warrant_df, _WARRANT_LEG_COLS)
            opt_row = _row_reader(opt_df, _OPTION_LEG_COLS)
        rows.append(_solved_row(warrant_row, opt_row, T_star, M, r, solved))
    return rows, dropped


def match_static_arb(stock_codes, min_volume=0, min_edge=0.0,
                     max_horizon_dte=None, r=None):
    """Scan every selected underlying for static arbs; returns a DataFrame."""
    stock_codes = list(stock_codes)
    r = options_logic.R if r is None else float(r)

    # Fetch once for every code (same pattern as arb_logic.match_warrant_tw_option):
    # read_warrant/read_tw_option filter server-side on `codes`, so a per-code loop
    # would pay a redundant round-trip per stock. No IV solve — the LP needs prices.
    all_warrant_df, warrant_err, warrant_meta = (
        warrant_logic.read_warrant(stock_codes, "All", 0, 365, 0, 1e9, 0, compute_iv=False)
        if stock_codes else (pd.DataFrame(), None, None)
    )
    all_opt_df, opt_err, opt_meta = (
        options_logic.read_tw_option(stock_codes, "All", min_days=1, compute_iv=False)
        if stock_codes else (pd.DataFrame(), None, None)
    )
    warrant_hard = (warrant_meta or {}).get("hard_error")
    opt_hard = (opt_meta or {}).get("hard_error")
    if not all_opt_df.empty:
        all_opt_df = all_opt_df[all_opt_df["ask_live"] | all_opt_df["bid_live"]]
        if min_volume > 0:
            all_opt_df = all_opt_df[all_opt_df["volume"] >= min_volume]

    all_rows, skip_reasons, hard_errors = [], [], []
    total_dropped = 0
    # One pass over each frame instead of an astype(str) over the whole column
    # per code (arb_logic.group_by_str keeps within-group row order).
    warrant_groups = arb_logic.group_by_str(all_warrant_df, "underlying_code")
    opt_groups = arb_logic.group_by_str(all_opt_df, "stock_code")

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic._commodity_map():
            skip_reasons.append(f"{code}: no options data available")
            continue
        M = options_logic._commodity_map()[code]["exercise_ratio"]

        warrant_df = warrant_groups.get(str(code), all_warrant_df.iloc[0:0])
        if warrant_df.empty:
            msg = f"{code}: {warrant_err or 'no warrants'}"
            (hard_errors if warrant_hard else skip_reasons).append(msg)
            continue

        opt_df = opt_groups.get(str(code), all_opt_df.iloc[0:0])
        if opt_df.empty:
            msg = f"{code}: {opt_err or 'no options'}"
            (hard_errors if opt_hard else skip_reasons).append(msg)
            continue

        spot = float(warrant_df.iloc[0]["underlying_price"])
        # Horizons are the dates a SHORT leg can settle on. The short side is
        # options only, so that set is exactly the option expiries.
        horizons = sorted(set(int(d) for d in opt_df["days_to_expiry"].unique()))
        if max_horizon_dte:
            horizons = [h for h in horizons if h <= int(max_horizon_dte)]

        if iv_engine.SCAN_STATIC_ARB and iv_engine.use_rust("arb"):
            code_rows, code_dropped = _scan_chain(warrant_df, opt_df, horizons,
                                                  M, r, min_edge)
        else:
            code_rows, code_dropped = [], 0
            for T_star in horizons:
                longs, shorts, dropped = _build_legs(warrant_df, opt_df, T_star, M, r)
                code_dropped += dropped
                row = _solve_horizon(longs, shorts, T_star, min_edge)
                if row:
                    code_rows.append(row)
        for row in code_rows:
            row["underlying_code"] = code
            row["underlying_price"] = round(spot, 2)

        total_dropped += code_dropped
        all_rows.extend(code_rows)
        applog.log("ARB", f"{code} {pos} horizons={len(horizons)} "
                          f"structures={len(code_rows)} dropped_no_depth={code_dropped}")

    if not all_rows:
        if total_dropped:
            # An empty scan reads very differently when depth was missing wholesale
            # (TAIFEX MIS down, EOD fallback carries no sizes) than when the chain
            # is simply arb-free, so say which one happened.
            skip_reasons.append(
                f"{total_dropped} legs dropped for missing resting size "
                "(TAIFEX MIS unavailable? EOD fallback carries no bid/ask size)")
        arb_logic._finish_empty_scan(hard_errors, skip_reasons)

    result = pd.DataFrame(all_rows).sort_values("guaranteed_profit", ascending=False)
    result.attrs["dropped_no_depth"] = total_dropped
    return result
