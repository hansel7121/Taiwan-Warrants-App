"""Pure-Python warrant-frame construction: CMoney payloads -> the scanner frame.

The BACKUP engine for the `warrant_frame` feature. `logic/iv_engine.py` picks
between this and the Rust builder in `rust/warrants_core/src/frame.rs`, and
`logic/warrant_logic.py` re-exports the winner, so callers never import from
here directly. Kept intact so the app still runs, and parity tests still have
something to compare against, when the extension is unavailable.

`COL_ORDER` lives here rather than in `warrant_logic` because both engines need
it and `warrant_logic` imports the engine, not the other way round.
"""
import numpy as np
import pandas as pd

from logic.iv_engine import (  # noqa: F401
    implied_vol, implied_vol_vec, bs_delta_vec, _refine_iv_for_rounding,
)

R_FREE_DEFAULT = 0.02

COL_ORDER = [
    "warrant_code",
    "warrant_name",
    "underlying_code",
    "type",
    "underlying_price",
    "ask",
    "bid",
    "ask_qty",
    "bid_qty",
    "days_to_expiry",
    "strike",
    "exercise_ratio",
    "volume",
    "time_value",
    "bid_time_value_pct",
    "ask_time_value_pct",
    "time_value_am",
    "iv_ask",
    "iv_bid",
    "delta_calc",
    "leverage_calc",
]


def _pct_of_spot(value, S):
    """`value` as a % of spot, rounded like the rest of the frame; NaN in, NaN out."""
    if value is None or not np.isfinite(value):
        return np.nan
    return round(value / S * 100, 4)


def build_warrant_df(cmoney_results, compute_iv=True, keep_noniv=False,
                     allow_no_quote=False):
    r_free_default = 0.02

    # Pass 1: parse + pre-filter each warrant into a base row WITHOUT the IV
    # solve, collecting the pricing inputs in parallel arrays. The scalar solve
    # was the dominant cost; deferring it lets one vectorized sweep replace
    # thousands of per-row brentq calls.
    rows = []
    a_ask = []; a_bid = []; a_S = []; a_K = []; a_T = []; a_r = []; a_ratio = []; a_put = []
    for code, data in cmoney_results.items():
        try:
            w = data["Warrant"]
            s = data["Stock"]

            # CMoney's Stock.CommKey is the authoritative underlying stock code
            # (e.g. "2645"), so the true underlying is verified here rather than
            # inferred from the abbreviated warrant display name.
            underlying_code = str(s.get("CommKey")) if s.get("CommKey") is not None else None
            underlying_price = float(s.get("SalePr") or 0)
            ask = float(w.get("SellPr1") or 0)
            bid = float(w.get("BuyPr1") or 0)
            # Best-level orderbook size (張). CMoney returns the full 5-level
            # depth (SellQty1..5 / BuyQty1..5); we keep only level 1 — the size
            # actually resting at the best ask/bid — so the arb finder can check
            # whether an arb's needed board_lots can be filled at the quoted price.
            ask_qty = int(w.get("SellQty1") or 0)   # 張 resting at best ask
            bid_qty = int(w.get("BuyQty1") or 0)    # 張 resting at best bid
            volume = int(w.get("SaleQty") or 0)
            warrant_name = w.get("CommName", "")
            days_to_expiry = int(w.get("LastDays") or 0)
            strike = float(w.get("StrikePr") or 0)
            exercise_ratio = float(w.get("UserRate") or 0)
            r_free = r_free_default

            is_put = int(w.get("CallorPut") or 1) == 2

            if underlying_price <= 0 or days_to_expiry <= 0:
                continue
            # Every ask-side metric divides by the ask, so a warrant quoting no
            # offer has none of them. Dropping those rows is what keeps a
            # "buy for free" phantom out of the arb paths; the scanner passes
            # allow_no_quote=True to see one-sided and empty books too.
            if ask <= 0 and not allow_no_quote:
                continue

            T = days_to_expiry / 365.0

            if is_put:
                intrinsic = max(0, strike - underlying_price) * exercise_ratio
                distance_to_strike = underlying_price - strike
            else:
                intrinsic = max(0, underlying_price - strike) * exercise_ratio
                distance_to_strike = strike - underlying_price
            # One formula per quote side: the quote / exercise ratio is the price
            # per underlying share, plus the distance to strike. An empty side
            # yields NaN rather than a number derived from a zero quote.
            time_value = (
                (ask / exercise_ratio) + distance_to_strike if ask > 0 else np.nan
            )
            bid_time_value = (
                (bid / exercise_ratio) + distance_to_strike if bid > 0 else np.nan
            )
            time_value_am = ask - intrinsic if ask > 0 else np.nan

            rows.append(
                {
                    "warrant_code": code,
                    "warrant_name": warrant_name,
                    "underlying_code": underlying_code,
                    "type": "Put" if is_put else "Call",
                    "underlying_price": underlying_price,
                    "ask": ask,
                    "bid": bid,
                    "ask_qty": ask_qty,
                    "bid_qty": bid_qty,
                    "days_to_expiry": days_to_expiry,
                    "strike": strike,
                    "exercise_ratio": exercise_ratio,
                    "volume": volume,
                    "time_value": round(time_value, 4),
                    "bid_time_value_pct": _pct_of_spot(bid_time_value, underlying_price),
                    "ask_time_value_pct": _pct_of_spot(time_value, underlying_price),
                    "time_value_am": round(time_value_am, 4),
                    # IV-derived metrics filled in below (positions fixed here so
                    # the column order stays COL_ORDER regardless of solve path).
                    "iv_ask": np.nan,
                    "iv_bid": np.nan,
                    "delta_calc": np.nan,
                    "leverage_calc": np.nan,
                }
            )
            a_ask.append(ask); a_bid.append(bid); a_S.append(underlying_price)
            a_K.append(strike); a_T.append(T); a_r.append(r_free)
            a_ratio.append(exercise_ratio); a_put.append(is_put)
        except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
            # A malformed CMoney payload drops its row. Named exceptions rather
            # than a bare `except`: this parse also runs in the Rust engine,
            # where "any Python exception" has no equivalent, and a silent
            # row-count divergence between the two would be invisible.
            # ZeroDivisionError is load-bearing, not defensive: a warrant
            # quoting an exercise ratio of 0 divides by it computing time value,
            # and has always been dropped here rather than by a guard.
            continue

    if not rows:
        return pd.DataFrame(columns=COL_ORDER)

    # Pass 2: one vectorized IV/delta/leverage solve over all surviving rows,
    # then the per-row post-logic (bid->ask fallback, keep_noniv, drop) applied
    # as array ops. round(...,4) is done element-wise with the builtin so the
    # rounding is byte-identical to the scalar path.
    n = len(rows)
    ask_arr = np.array(a_ask); bid_arr = np.array(a_bid); S_arr = np.array(a_S)
    K_arr = np.array(a_K); T_arr = np.array(a_T); r_arr = np.array(a_r)
    ratio_arr = np.array(a_ratio); put_arr = np.array(a_put, dtype=bool)

    if compute_iv:
        iv_ask = implied_vol_vec(ask_arr, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr)
        # Re-solve any row whose rounded delta OR leverage (both derived from the
        # unrounded iv_ask, exactly as the scalar path does) could flip between
        # Newton's and brentq's root, so those columns match the scalar path
        # bit-for-bit. Leverage denominator is the ask price.
        iv_ask = _refine_iv_for_rounding(
            iv_ask, ask_arr, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr,
            check_delta=True, check_leverage=True, lev_price=ask_arr,
        )
        bid_price = np.where(bid_arr > 0, bid_arr, np.nan)
        iv_bid = implied_vol_vec(bid_price, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr)
        converged = ~np.isnan(iv_ask)
        # iv_bid falls back to iv_ask on converged rows where bid IV is NaN.
        iv_bid = np.where(converged & np.isnan(iv_bid), iv_ask, iv_bid)
        delta = bs_delta_vec(S_arr, K_arr, T_arr, r_arr, iv_ask, ratio_arr, put_arr)
        with np.errstate(all="ignore"):
            # ask can be 0 under allow_no_quote; delta is already NaN there
            # (NaN sigma), so the division stays NaN rather than inf.
            leverage = S_arr * np.abs(delta) / ask_arr
        # Non-converged rows carry all-NaN IV metrics (drop or keep_noniv).
        iv_bid = np.where(converged, iv_bid, np.nan)
        delta = np.where(converged, delta, np.nan)
        leverage = np.where(converged, leverage, np.nan)
        # A no-ask row has no price to solve, so it can never converge — keeping
        # it explicitly is what stops the IV filter from erasing exactly the
        # one-sided books allow_no_quote let in. No-op when ask>0 is enforced.
        keep = np.ones(n, dtype=bool) if keep_noniv else (converged | (ask_arr <= 0))
    else:
        # Arb finder does not use IV/delta/leverage — skip the solve entirely.
        iv_ask = iv_bid = delta = leverage = np.full(n, np.nan)
        keep = np.ones(n, dtype=bool)

    final_rows = []
    for i in range(n):
        if not keep[i]:
            continue
        d = rows[i]
        d["iv_ask"] = round(float(iv_ask[i]), 4)
        d["iv_bid"] = round(float(iv_bid[i]), 4)
        d["delta_calc"] = round(float(delta[i]), 4)
        d["leverage_calc"] = round(float(leverage[i]), 4)
        final_rows.append(d)

    if not final_rows:
        return pd.DataFrame(columns=COL_ORDER)
    return pd.DataFrame(final_rows)[COL_ORDER]
