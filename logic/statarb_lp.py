"""Statistical-arbitrage LP for the Arb Finder → StatArb sub-tab (#9): logic/static_arb.py's per-horizon LP with
"payoff >= 0 at every spot" relaxed to "payoff >= 0 on a spot band holding >= 1 - cap of every simulated path set",
so P(loss at the horizon) <= cap. Driven by statarb_logic.scan_lp.

Only the spot at the horizon matters, and the payoff is piecewise linear in it, so "P&L >= 0 on [lo, hi]" is exact
with one constraint at each edge and at every kink inside — the same trick static_arb uses for [0, inf). The cap is
split between the two tails in BAND_SPLITS ways; each split is one small MILP in whole lots (warrant 張, option 口 —
so no after-the-fact rounding) and the best result wins. A max-loss
budget on every kink plus a non-negative tail slope bounds the loss outside the band too, and keeps the LP from
scaling into full quote depth. The objective is entry credit (as static_arb) or expected P&L on the zero-drift set;
entry credit must be >= 0 either way. Bands come from the first half of each path set and the result is re-scored
on the second half, so the reported P(loss) is out of sample. Legs come from static_arb._build_legs.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from logic import static_arb

BAND_SPLITS = 11        # ways to split the cap between the lower and upper tail
TIME_LIMIT_S = 2.0      # MILP time cap per band; the best whole-lot solution found so far is kept
OBJECTIVES = ("credit", "expected")


def payoff_matrix(legs, spots):
    """(len(spots), len(legs)) per-share payoff of each leg at each spot (longs at their floor, as static_arb)."""
    if not legs:
        return np.zeros((len(spots), 0))
    k = np.array([l["eff_strike"] for l in legs])
    call = np.array([l["is_call"] for l in legs])
    s = np.asarray(spots, dtype=float)[:, None]
    return np.where(call, np.maximum(s - k, 0.0), np.maximum(k - s, 0.0))


def pnl_at(longs, shorts, xl, xs, spots):
    """Total P&L (credit plus payoff) per spot for share weights xl (longs) and xs (shorts)."""
    credit = sum(s["price_ps"] * w for s, w in zip(shorts, xs)) - sum(l["price_ps"] * w for l, w in zip(longs, xl))
    return credit + payoff_matrix(longs, spots) @ np.asarray(xl, float) - payoff_matrix(shorts, spots) @ np.asarray(xs, float)


def bands(fit_sets, cap, splits=BAND_SPLITS):
    """[(lo, hi)] spot bands each holding >= 1 - cap of every path set, one per lower-tail share of the cap."""
    out = []
    for a in np.linspace(0.0, cap, splits):
        lo = min(float(np.quantile(v, a)) for v in fit_sets.values()) if a > 0 else 0.0
        hi = max(float(np.quantile(v, 1 - (cap - a))) for v in fit_sets.values()) if a < cap else np.inf
        out.append((lo, hi))
    return out


def _solve_milp(longs, shorts, band, max_loss, objective, zero, time_limit):
    """One band's MILP in whole lots -> scipy result. Variables: long lots, then short lots."""
    lo, hi = band
    legs = list(longs) + list(shorts)
    sign = np.r_[np.ones(len(longs)), -np.ones(len(shorts))]          # +1 long, -1 short
    lot = np.array([l["lot_shares"] for l in legs])
    price = np.array([l["price_ps"] for l in legs])
    cost = sign * price * lot                                          # cash out per lot (negative = proceeds)
    c = cost.copy()
    if objective == "expected":
        c -= sign * payoff_matrix(legs, zero).mean(axis=0) * lot

    def loss_rows(spots):
        return cost - sign * payoff_matrix(legs, spots) * lot

    kinks = np.array(static_arb._kink_points(longs, shorts))
    inside = np.r_[[lo], kinks[(kinks > lo) & (kinks < hi)], [hi] if np.isfinite(hi) else []]
    slope = -sign * np.array([static_arb._leg_slope(l) for l in legs]) * lot
    A = np.vstack([loss_rows(inside),          # loss <= 0 on the band
                   loss_rows(kinks),           # loss <= max_loss everywhere
                   slope,                      # far-right slope >= 0
                   cost])                      # entry credit >= 0
    b = np.r_[np.zeros(len(inside)), np.full(len(kinks), max_loss), 0.0, 0.0]
    # Rows span ~1e-2 to ~1e7 (per-share quotes x 2,000-share lots x strikes): normalise each to unit max coefficient.
    # HiGHS's MIP presolve also declared real chain models infeasible although "trade nothing" satisfies every row,
    # so it is switched off below (the models are ~1k columns by ~50 rows, fast without it).
    scale = np.abs(A).max(axis=1)
    scale[scale == 0] = 1.0
    A, b = A / scale[:, None], b / scale
    depth = np.array([l["depth_lots"] for l in legs], dtype=float)
    return milp(c, constraints=LinearConstraint(A, -np.inf, b), integrality=np.ones(len(legs)),
                bounds=Bounds(0, depth), options={"time_limit": time_limit, "mip_rel_gap": 1e-4, "presolve": False})


def _p_loss(longs, shorts, xl, xs, end_sets):
    """{set: share of horizon spots with P&L < 0}."""
    return {k: round(float((pnl_at(longs, shorts, xl, xs, v) < 0).mean()), 6) for k, v in end_sets.items()}


def _solve_band(longs, shorts, band, cap, max_loss, objective, fit_sets, check_sets, min_edge, time_limit):
    """Whole-lot structure for one band, scored in and out of sample, or None."""
    zero = fit_sets.get("zero", next(iter(fit_sets.values())))
    if not longs or not shorts:
        return None
    res = _solve_milp(longs, shorts, band, max_loss, objective, zero, time_limit)
    if res.x is None or -float(res.fun) <= static_arb._TOL:
        return None
    lots = np.round(res.x).astype(int)
    nL = len(longs)
    keep_l = [{**l, "lots": int(n), "shares": n * l["lot_shares"]} for l, n in zip(longs, lots[:nL]) if n > 0]
    keep_s = [{**s, "lots": int(n), "shares": n * s["lot_shares"]} for s, n in zip(shorts, lots[nL:]) if n > 0]
    if not keep_s:
        return None
    xl, xs = [l["shares"] for l in keep_l], [s["shares"] for s in keep_s]
    kinks = np.array(static_arb._kink_points(keep_l, keep_s))
    at_kinks = pnl_at(keep_l, keep_s, xl, xs, kinks)
    tail = sum(w * static_arb._leg_slope(l) for l, w in zip(keep_l, xl)) - sum(
        w * static_arb._leg_slope(s) for s, w in zip(keep_s, xs))
    credit = sum(s["price_ps"] * w for s, w in zip(keep_s, xs)) - sum(l["price_ps"] * w for l, w in zip(keep_l, xl))
    if credit < max(min_edge, static_arb._TOL):
        return None
    p_chk = _p_loss(keep_l, keep_s, xl, xs, check_sets)
    return {
        "legs": [static_arb._leg_out(l, "long") for l in keep_l] + [static_arb._leg_out(s, "short") for s in keep_s],
        "n_long": len(keep_l), "n_short": len(keep_s),
        "net_credit": round(credit), "max_loss": round(min(float(at_kinks.min()), 0.0)),
        "worst_spot": round(float(kinks[int(np.argmin(at_kinks))]), 2),
        "expected_pnl": round(float(pnl_at(keep_l, keep_s, xl, xs,
                                           check_sets.get("zero", next(iter(check_sets.values())))).mean())),
        "band": [round(band[0], 2), None if not np.isfinite(band[1]) else round(band[1], 2)],
        "p_fit": _p_loss(keep_l, keep_s, xl, xs, fit_sets), "p_expiry": p_chk,
        "passes": max(p_chk.values()) <= cap,
        "pure": float(at_kinks.min()) >= -static_arb._TOL and tail >= -static_arb._TOL,
        "optimal": bool(res.status == 0),
    }


def solve_horizon(longs, shorts, fit_sets, check_sets, cap, max_loss, objective="credit", min_edge=0.0,
                  time_limit=TIME_LIMIT_S):
    """Best whole-lot structure at one horizon across every band split, or None.

    fit_sets / check_sets: {path set name: horizon spots} from disjoint halves of the paths. Rows that pass out of
    sample rank first, then by the objective.
    """
    def one(band):
        return _solve_band(longs, shorts, band, cap, max_loss, objective, fit_sets, check_sets, min_edge, time_limit)

    with ThreadPoolExecutor(max_workers=min(BAND_SPLITS, os.cpu_count() or 1)) as ex:   # HiGHS releases the GIL
        results = list(ex.map(one, bands(fit_sets, cap)))
    best, best_key = None, None
    for row in results:
        if row is None:
            continue
        key = (row["passes"], row["net_credit"] if objective == "credit" else row["expected_pnl"])
        if best_key is None or key > best_key:
            best, best_key = row, key
    return best
