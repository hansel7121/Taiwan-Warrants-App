"""The static-arbitrage LP's soundness guarantees.

Four properties are pinned here, because each one is a way the LP could silently
certify money that is not there: an arbitrage-free chain must yield nothing, a
planted convexity break must be found with the exact hand-computed profit, a
long European put must never be floored at intrinsic, and the integer-repaired
weights must still pay off >= 0 at every kink.
"""
import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from logic import static_arb

M = 2000        # TW single-stock option contract size (shares)
R = 0.01875     # options_logic.R


def bs(S, K, T, r, sig, put=False):
    """Black-Scholes price — the reference arbitrage-free price system."""
    if T <= 0:
        return max(0.0, (K - S) if put else (S - K))
    d1 = (np.log(S / K) + (r + sig * sig / 2) * T) / (sig * np.sqrt(T))
    d2 = d1 - sig * np.sqrt(T)
    if put:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def warrant(code, typ, strike, dte, ask, ratio=1.0, ask_qty=10, spot=110.0):
    return {
        "warrant_code": code, "warrant_name": f"W{code}", "underlying_code": "2330",
        "type": typ, "underlying_price": spot, "ask": ask, "bid": ask * 0.98,
        "ask_qty": ask_qty, "bid_qty": ask_qty, "days_to_expiry": dte,
        "strike": strike, "exercise_ratio": ratio, "volume": 100,
    }


def option(contract, typ, strike, dte, bid=None, ask=None,
           bid_size=10, ask_size=10, spot=110.0):
    return {
        "contract": contract, "stock_code": "2330", "type": typ,
        "underlying_price": spot, "strike": strike, "days_to_expiry": dte,
        "bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
        "exercise_ratio": M, "volume": 50, "oi": 500,
        "bid_live": bid is not None, "ask_live": ask is not None,
    }


def solve(warrants, options, T_star, min_edge=0.0):
    """Run the two internal stages the way match_static_arb does."""
    wdf = pd.DataFrame(warrants) if warrants else pd.DataFrame()
    odf = pd.DataFrame(options) if options else pd.DataFrame()
    longs, shorts, dropped = static_arb._build_legs(wdf, odf, T_star, M, R)
    return static_arb._solve_horizon(longs, shorts, T_star, min_edge), dropped


# ── an arbitrage-free chain must produce nothing ─────────────────────────────

def test_black_scholes_chain_yields_no_arb():
    """Priced at exact BS with zero spread, every payoff->=0 portfolio costs >= 0,
    so the LP optimum cannot be a credit. This is the false-positive guard."""
    S, T, sig, dte = 110.0, 30 / 365.0, 0.35, 30
    warrants, options = [], []
    for K in (90.0, 100.0, 110.0, 120.0, 130.0):
        for typ, put in (("Call", False), ("Put", True)):
            p = bs(S, K, T, R, sig, put)
            options.append(option(f"{typ[0]}{int(K)}", typ, K, dte, bid=p, ask=p))
            warrants.append(warrant(f"W{typ[0]}{int(K)}", typ, K, dte, ask=p))

    row, _ = solve(warrants, options, dte)
    assert row is None


# ── a planted convexity break must be found exactly ──────────────────────────

def test_planted_convexity_break_is_found_with_exact_profit():
    """Long wings at 100/120 vs a body short at 110 priced far too rich.

    Hand-computed optimum: both wings at their full 10 張 depth (10,000 shares
    each), body short capped by the tail-slope constraint at 20,000 shares
    (10 contracts). Credit = 8*20000 - 3*10000 - 2*10000 = 110,000 NT$, and the
    payoff floor bottoms at exactly 0.
    """
    dte = 30
    warrants = [
        warrant("W100", "Call", 100.0, dte, ask=3.0, ratio=1.0, ask_qty=10),
        warrant("W120", "Call", 120.0, dte, ask=2.0, ratio=1.0, ask_qty=10),
    ]
    options = [option("C110", "Call", 110.0, dte, bid=8.0, bid_size=10)]

    row, _ = solve(warrants, options, dte)

    assert row is not None
    assert row["n_legs"] == 3
    assert row["n_long"] == 2 and row["n_short"] == 1
    assert row["net_credit"] == pytest.approx(110_000)
    assert row["min_payoff"] == pytest.approx(0.0)
    assert row["guaranteed_profit"] == pytest.approx(110_000)

    shorts = [l for l in row["legs"] if l["side"] == "short"]
    assert len(shorts) == 1
    assert shorts[0]["lots"] == 10 and shorts[0]["lot_label"] == "口"
    longs = {l["code"]: l for l in row["legs"] if l["side"] == "long"}
    assert longs["W100"]["lots"] == 10 and longs["W100"]["lot_label"] == "張"
    assert longs["W120"]["lots"] == 10


# ── the European-put floor, the trap this whole design turns on ──────────────

def test_long_european_put_is_not_floored_at_intrinsic():
    """Short a 30d put, hold a cheaper 120d put at the same strike.

    Under an intrinsic floor the two payoffs cancel exactly and the price gap
    reads as free money. They do not cancel: a European put's floor is
    (K*d - S)+, which sits BELOW intrinsic, so the structure is short the
    difference and loses K*(1-d) at S=0. Must be rejected.
    """
    K, T_star = 100.0, 30
    options = [
        option("P100-30", "Put", K, T_star, bid=6.00),
        option("P100-120", "Put", K, 120, ask=5.90),
    ]

    row, _ = solve([], options, T_star)
    assert row is None

    # And prove the rejection is the floor's doing, not an empty leg set.
    longs, shorts, _ = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), T_star, M, R)
    assert len(longs) == 1 and len(shorts) == 1
    far = longs[0]
    assert far["eff_strike"] < far["strike"]          # discounted, not intrinsic
    assert static_arb._leg_payoff(far, 0.0) < static_arb._leg_payoff(shorts[0], 0.0)


def test_call_floor_uses_the_discounted_strike():
    """The mirror case: a long call's bound is (S - K*d)+, tighter than intrinsic
    and still sound, so the kink sits below the nominal strike."""
    options = [
        option("C100-30", "Call", 100.0, 30, bid=4.0, ask=4.2),
        option("C100-120", "Call", 100.0, 120, ask=8.0),
    ]
    longs, _, _ = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), 30, M, R)
    far = [l for l in longs if l["dte"] == 120][0]
    near = [l for l in longs if l["dte"] == 30][0]
    assert far["eff_strike"] == pytest.approx(100.0 * np.exp(-R * 90 / 365.0))
    assert near["eff_strike"] == pytest.approx(100.0)   # d = 1 at the horizon


# ── integer repair must not break the payoff floor ───────────────────────────

def test_integer_repaired_weights_still_pay_off_everywhere():
    """Depths chosen so the LP optimum is fractional in lots; the emitted integer
    weights must still satisfy payoff >= 0 at every kink, recomputed here from
    the reported lots rather than trusting the solver."""
    dte = 30
    warrants = [
        warrant("W100", "Call", 100.0, dte, ask=3.0, ratio=0.7, ask_qty=7),
        warrant("W120", "Call", 120.0, dte, ask=2.0, ratio=0.3, ask_qty=13),
    ]
    options = [option("C110", "Call", 110.0, dte, bid=8.0, bid_size=9)]

    row, _ = solve(warrants, options, dte)
    assert row is not None

    def payoff(S):
        total = 0.0
        for leg in row["legs"]:
            sign = -1.0 if leg["side"] == "short" else 1.0
            k = leg["eff_strike"]
            intrinsic = max(0.0, S - k) if leg["type"] == "Call" else max(0.0, k - S)
            total += sign * leg["shares"] * intrinsic
        return total

    kinks = [0.0] + sorted(l["eff_strike"] for l in row["legs"])
    for S in kinks + [max(kinks) * 2]:
        assert payoff(S) >= -1e-6, f"negative payoff at S={S}"

    assert row["net_credit"] > 0
    # Every long leg rounded UP to a whole lot and still fits its resting depth.
    for leg in row["legs"]:
        if leg["side"] == "long":
            lot = 1000.0 * leg["ratio"] if leg["kind"] == "warrant" else float(M)
            assert leg["shares"] == pytest.approx(leg["lots"] * lot)
            assert leg["lots"] <= leg["depth_lots"]


def test_min_edge_filters_thin_structures():
    dte = 30
    warrants = [
        warrant("W100", "Call", 100.0, dte, ask=3.0, ratio=1.0, ask_qty=10),
        warrant("W120", "Call", 120.0, dte, ask=2.0, ratio=1.0, ask_qty=10),
    ]
    options = [option("C110", "Call", 110.0, dte, bid=8.0, bid_size=10)]

    assert solve(warrants, options, dte, min_edge=0.0)[0] is not None
    assert solve(warrants, options, dte, min_edge=200_000)[0] is None


# ── depth gating ─────────────────────────────────────────────────────────────

def test_short_leg_without_resting_size_is_excluded_and_counted():
    dte = 30
    options = [
        option("C110", "Call", 110.0, dte, bid=8.0, bid_size=None),
        option("C120", "Call", 120.0, dte, bid=4.0, bid_size=5),
    ]
    _, shorts, dropped = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), dte, M, R)

    assert dropped == 1
    assert [s["code"] for s in shorts] == ["C120"]


def test_settlement_marked_quotes_are_never_traded():
    """bid_live/ask_live are flagged before the settlement fallback backfills a
    missing side, so a stale settlement mark must not become a tradable leg."""
    dte = 30
    options = [option("C110", "Call", 110.0, dte, bid=None, ask=None)]
    options[0]["bid"] = 8.0     # settlement backfill, both _live flags still False
    options[0]["ask"] = 8.5

    longs, shorts, _ = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), dte, M, R)
    assert longs == [] and shorts == []


# ── horizon partition ────────────────────────────────────────────────────────

def test_shorts_must_expire_exactly_at_the_horizon():
    """A short expiring before the horizon settled against a different spot and
    cannot enter a one-period LP; one expiring after would outlive its cover."""
    options = [
        option("C110-20", "Call", 110.0, 20, bid=4.0),
        option("C110-30", "Call", 110.0, 30, bid=5.0),
        option("C110-60", "Call", 110.0, 60, bid=7.0),
    ]
    _, shorts, _ = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), 30, M, R)
    assert [s["code"] for s in shorts] == ["C110-30"]


def test_longs_may_outlive_the_horizon_but_not_precede_it():
    options = [
        option("C110-20", "Call", 110.0, 20, ask=4.0),
        option("C110-30", "Call", 110.0, 30, ask=5.0),
        option("C110-60", "Call", 110.0, 60, ask=7.0),
    ]
    longs, _, _ = static_arb._build_legs(
        pd.DataFrame(), pd.DataFrame(options), 30, M, R)
    assert sorted(l["code"] for l in longs) == ["C110-30", "C110-60"]
