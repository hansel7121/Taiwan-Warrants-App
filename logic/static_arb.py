"""Static-arbitrage LP over a whole Taiwan warrant + option chain.

Where arb_logic hand-codes one structure per model (vertical, put-call parity,
butterfly) with fixed weights, this module solves for the weights instead: one
linear program per (underlying, horizon), maximising the entry credit subject to
the portfolio's payoff being >= 0 at every spot. Within the class of static
buy-and-hold portfolios of quoted instruments sharing one horizon it is
complete — every such arb is found, and an empty result is a proof that none
exists. arb_logic's three models all fall out as weight-restricted special
cases. Drives two Arb Finder sub-tabs via app.py::match_static_arb — Experiment
(warrants buy-only, the original model) and Short Warrant LP
(allow_short_warrants=True, which also lets the LP SELL a warrant into its
resting bid and so reaches arb_logic's "Buy Option / Sell Warrant" direction).
Scoped to Taiwan warrants vs Taiwan options.
"""
import numpy as np
import pandas as pd
from scipy.optimize import linprog

from services import applog
from logic import arb_logic
from logic import options_logic
from logic import warrant_logic

# Every long leg is floored at its EUROPEAN lower bound, (S - K*d)+ for calls and
# (K*d - S)+ for puts. The put case is the whole reason: a European put may trade
# BELOW intrinsic, so flooring one at (K - S)+ would certify arbs that do not
# exist. Warrants are American-exercisable and would justify the intrinsic floor
# on puts, but they cash-settle against a reference price rather than the
# instantaneous spot, so the European bound is the only unconditionally provable
# floor. Flip only if you accept that settlement caveat.
AMERICAN_PUT_INTRINSIC_FLOOR = False

# Short warrant legs are CALL-ONLY, and this is a soundness bound, not a filter.
#
# Selling a warrant is shorting an AMERICAN-exercisable claim: the holder may
# exercise before T_star, at which point the one-period payoff model stops being
# a proof. For a short CALL on a non-dividend-paying underlying early exercise is
# never optimal, so the horizon payoff is still the binding case and the proof
# survives. For a short PUT it is not — early exercise can be optimal, and this
# app models no dividends at all, so a short-put structure would be presented as
# riskless without being provable. Widen only alongside a dividend model.
SHORT_WARRANTS_CALL_ONLY = True

# LP weights below this fraction of the largest weight are numerical dust.
DUST_FRAC = 1e-3

_TOL = 1e-6


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


def _build_legs(warrant_df, opt_df, T_star, M, r, allow_short_warrants=False):
    """Normalise both chains to per-underlying-share legs at horizon T_star.

    Shorts must expire at EXACTLY T_star — one that settled earlier settled
    against the spot at its own expiry, a different random variable that a
    one-period LP cannot represent. Longs may expire at or beyond T_star and are
    replaced by their lower bound there.

    ``allow_short_warrants`` adds the warrant sell side (see
    SHORT_WARRANTS_CALL_ONLY). Off by default so the original buy-only model is
    unchanged for callers that do not opt in.
    """
    longs, shorts, dropped_no_depth = [], [], 0

    for _, w in warrant_df.iterrows():
        dte = int(w["days_to_expiry"])
        if dte < T_star:
            continue
        ratio = float(w.get("exercise_ratio") or 0)
        if ratio <= 0:
            continue
        lot_shares = 1000.0 * ratio   # one 張 = 1,000 units, each delivering `ratio` shares
        base = {
            "kind": "warrant", "code": str(w["warrant_code"]),
            "name": str(w["warrant_name"]), "type": w["type"],
            "is_call": w["type"] == "Call", "strike": float(w["strike"]),
            "dte": dte, "ratio": ratio, "lot_shares": lot_shares,
        }

        ask = float(w["ask"]) if pd.notna(w.get("ask")) else 0.0
        qty = _int_or(w.get("ask_qty"))
        if ask > 0:
            if qty <= 0:
                dropped_no_depth += 1
            else:
                longs.append({**base, "quote": round(ask, 4),
                              "price_ps": round(ask / ratio, 6), "depth_lots": qty,
                              "depth_shares": qty * lot_shares})

        # Short warrants, same horizon rule as short options: sold into the
        # resting bid, and only at EXACTLY T_star. One outliving the horizon
        # would have to be bought back at an unknown price, which a one-period
        # model cannot bound; one expiring earlier settled against a different
        # spot. Calls only — see SHORT_WARRANTS_CALL_ONLY.
        if (allow_short_warrants and dte == T_star
                and not (SHORT_WARRANTS_CALL_ONLY and base["is_call"] is False)):
            bid = float(w["bid"]) if pd.notna(w.get("bid")) else 0.0
            bqty = _int_or(w.get("bid_qty"))
            if bid > 0:
                if bqty <= 0:
                    dropped_no_depth += 1
                else:
                    shorts.append({**base, "quote": round(bid, 4),
                                   "price_ps": round(bid / ratio, 6), "depth_lots": bqty,
                                   "depth_shares": bqty * lot_shares})

    for _, o in opt_df.iterrows():
        dte = int(o["days_to_expiry"])
        typ = o["type"]
        K = float(o["strike"])
        base = {
            "kind": "option", "code": str(o["contract"]), "name": str(o["contract"]),
            "type": typ, "is_call": typ == "Call", "strike": K, "dte": dte,
            "ratio": None, "lot_shares": float(M),
        }

        if dte == T_star and bool(o.get("bid_live", False)):
            bid = float(o["bid"]) if pd.notna(o.get("bid")) else 0.0
            size = _int_or(o.get("bid_size"))
            if bid > 0:
                if size <= 0:
                    dropped_no_depth += 1
                else:
                    shorts.append({**base, "quote": round(bid, 4),
                                   "price_ps": round(bid, 6), "depth_lots": size,
                                   "depth_shares": size * float(M)})

        if dte >= T_star and bool(o.get("ask_live", False)):
            ask = float(o["ask"]) if pd.notna(o.get("ask")) else 0.0
            size = _int_or(o.get("ask_size"))
            if ask > 0:
                if size <= 0:
                    dropped_no_depth += 1
                else:
                    longs.append({**base, "quote": round(ask, 4),
                                  "price_ps": round(ask, 6), "depth_lots": size,
                                  "depth_shares": size * float(M)})

    for leg in longs:
        tau = max(0.0, (leg["dte"] - T_star) / 365.0)
        disc = float(np.exp(-r * tau))
        if AMERICAN_PUT_INTRINSIC_FLOOR and leg["kind"] == "warrant" and not leg["is_call"]:
            disc = 1.0
        leg["disc"] = disc
        leg["eff_strike"] = leg["strike"] * disc
    for leg in shorts:
        leg["disc"] = 1.0
        leg["eff_strike"] = leg["strike"]

    return longs, shorts, dropped_no_depth


def _repair_integers(longs, shorts, xl_raw, xs_raw, dust):
    """Drop dust, round shorts DOWN and longs UP to whole tradable lots.

    Direction matters: floors are non-negative and non-decreasing in the long
    weights, so rounding longs up only lifts the payoff curve while rounding
    shorts down only shrinks what is subtracted. Payoff >= 0 therefore survives
    the rounding by construction — but dropping a dust LONG removes protection,
    so the caller still re-verifies every kink.
    """
    keep_l, xl = [], []
    for leg, v in zip(longs, xl_raw):
        if v <= dust:
            continue
        lots = int(np.ceil(v / leg["lot_shares"] - _TOL))
        shares = lots * leg["lot_shares"]
        if lots <= 0 or shares > leg["depth_shares"] + _TOL:
            return None, None, None, None   # can't buy enough protection
        keep_l.append({**leg, "lots": lots, "shares": shares})
        xl.append(shares)

    keep_s, xs = [], []
    for leg, v in zip(shorts, xs_raw):
        if v <= dust:
            continue
        lots = int(np.floor(v / leg["lot_shares"] + _TOL))
        if lots <= 0:
            continue
        shares = lots * leg["lot_shares"]
        keep_s.append({**leg, "lots": lots, "shares": shares})
        xs.append(shares)

    return keep_l, xl, keep_s, xs


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


def _solve_horizon(longs, shorts, T_star, min_edge):
    """Solve one horizon's LP and return a finished row, or None.

    maximise  sum(y_j * bid_j) - sum(x_i * cost_i)          [entry credit]
    s.t.      sum(x_i * floor_i(S)) - sum(y_j * payoff_j(S)) >= 0  at every kink
              same with slopes, for S -> infinity
              0 <= x_i <= depth_i,  0 <= y_j <= depth_j

    Every leg payoff is piecewise-linear with one kink, so the sum bends only at
    {eff_strike}. A piecewise-linear function is >= 0 everywhere iff it is >= 0
    at S=0, at every kink, and its far-right slope is >= 0 — so this finite
    constraint set is EXACTLY equivalent to the infinite condition, not a sample.
    """
    if not longs or not shorts:
        return None

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
    if not res.success or res.x is None:
        return None
    if -float(res.fun) <= max(min_edge, _TOL):
        return None

    z = np.asarray(res.x, dtype=float)
    dust = float(z.max()) * DUST_FRAC if z.size else 0.0
    keep_l, xl, keep_s, xs = _repair_integers(longs, shorts, z[:nL], z[nL:], dust)
    if not keep_l or not keep_s:
        return None

    # Re-verify from scratch on the integer weights — the proof above covers the
    # rounding but not the dropped dust longs.
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
    if credit <= 0 or guaranteed <= min_edge:
        return None

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


def match_static_arb(stock_codes, min_volume=0, min_edge=0.0,
                     max_horizon_dte=None, r=None, allow_short_warrants=False):
    """Scan every selected underlying for static arbs; returns a DataFrame.

    ``allow_short_warrants`` drives the "Short Warrant LP" sub-tab; the original
    Experiment sub-tab leaves it off and gets the identical buy-only model.
    """
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

    for i, code in enumerate(stock_codes, 1):
        pos = f"({i}/{len(stock_codes)})"
        if code not in options_logic._commodity_map():
            skip_reasons.append(f"{code}: no options data available")
            continue
        M = options_logic._commodity_map()[code]["exercise_ratio"]

        warrant_df = (all_warrant_df[all_warrant_df["underlying_code"].astype(str) == str(code)]
                      if not all_warrant_df.empty else all_warrant_df)
        if warrant_df.empty:
            msg = f"{code}: {warrant_err or 'no warrants'}"
            (hard_errors if warrant_hard else skip_reasons).append(msg)
            continue

        opt_df = (all_opt_df[all_opt_df["stock_code"].astype(str) == str(code)]
                  if not all_opt_df.empty else all_opt_df)
        if opt_df.empty:
            msg = f"{code}: {opt_err or 'no options'}"
            (hard_errors if opt_hard else skip_reasons).append(msg)
            continue

        spot = float(warrant_df.iloc[0]["underlying_price"])
        # Horizons are the dates a SHORT leg can settle on. With warrants
        # buy-only that was exactly the option expiries; once a warrant can be
        # sold, its own expiry is a horizon too, and skipping it would hide
        # every short-warrant structure.
        horizon_set = set(int(d) for d in opt_df["days_to_expiry"].unique())
        if allow_short_warrants:
            horizon_set |= set(int(d) for d in warrant_df["days_to_expiry"].unique())
        horizons = sorted(horizon_set)
        if max_horizon_dte:
            horizons = [h for h in horizons if h <= int(max_horizon_dte)]

        code_rows, code_dropped = [], 0
        for T_star in horizons:
            longs, shorts, dropped = _build_legs(warrant_df, opt_df, T_star, M, r,
                                                 allow_short_warrants)
            code_dropped += dropped
            row = _solve_horizon(longs, shorts, T_star, min_edge)
            if row:
                row["underlying_code"] = code
                row["underlying_price"] = round(spot, 2)
                code_rows.append(row)

        total_dropped += code_dropped
        all_rows.extend(code_rows)
        applog.log("ARB", f"{code} {pos} horizons={len(horizons)} "
                          f"structures={len(code_rows)} dropped_no_depth={code_dropped} "
                          f"short_warrants={allow_short_warrants}")

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
