"""Does the static-arb LP capture every arb Direct Match reports?

The LP's docstring claims completeness over static buy-and-hold portfolios, so
Direct Match's vertical should fall out as a weight-restricted special case.
Ten single-warrant / single-option pairs test that, each a clear buy-warrant /
sell-option arb.

It holds for calls unconditionally, and for puts exactly when
`static_arb.AMERICAN_PUT_INTRINSIC_FLOOR` is on.

That flag is the whole story. A long leg outliving the horizon is replaced by a
lower bound there. Warrants are American-exercisable, so the binding bound is
the immediate exercise value `(K - S)+`; the European bound `(K*exp(-r*tau) -
S)+` is what you may only assume when early exercise is unavailable. For a CALL
the discount lowers the long strike and lifts the payoff, so the choice never
matters. For a PUT it lowers the payoff, and the European bound understates an
American claim — which shows up as the LP refusing structures Direct correctly
reports.

With the flag off the LP also rejects same-strike put pairs where the warrant is
merely longer-dated, which is an ordinary shape in a real chain. The threshold
is exact: it accepts only while `Kw*exp(-r*tau) >= Ko`, a cushion of 0.15% per
30 days of extra warrant life.
"""
import numpy as np
import pandas as pd
import pytest

from logic import arb_logic, options_logic, static_arb

M = 2000                      # TW single-stock option contract = 2000 shares
R = options_logic.R
SPOT = 600.0


def warrant(code, typ, strike, ask, dte, ratio, ask_qty=200):
    return {"warrant_code": code, "warrant_name": f"W-{code}", "underlying_code": "2330",
            "type": typ, "underlying_price": SPOT, "ask": ask, "bid": ask * 0.98,
            "ask_qty": ask_qty, "bid_qty": ask_qty, "days_to_expiry": dte,
            "strike": strike, "exercise_ratio": ratio, "volume": 1000,
            "iv_ask": np.nan, "iv_bid": np.nan}


def option(contract, typ, strike, bid, dte, bid_size=200):
    return {"contract": contract, "type": typ, "underlying_price": SPOT,
            "ask": bid * 1.02, "bid": bid, "days_to_expiry": dte, "strike": strike,
            "exercise_ratio": M, "bid_size": bid_size, "ask_size": bid_size,
            "volume": 500, "oi": 500, "is_live": True, "ask_live": True,
            "bid_live": True, "stock_code": "2330", "iv_ask": np.nan, "iv_bid": np.nan}


# Each pair: buy the warrant at its ask, sell the option into its bid. Per
# underlying share the warrant costs ask/ratio and the option pays bid.
CASES = [
    ("call_same_expiry",      warrant("W1", "Call", 580.0, 12.0, 30, 0.5),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("call_equal_strikes",    warrant("W2", "Call", 600.0, 12.0, 30, 0.5),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("call_warrant_outlives", warrant("W3", "Call", 580.0, 12.0, 90, 0.5),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("put_same_expiry",       warrant("W4", "Put", 620.0, 12.0, 30, 0.5),
                              option("P600", "Put", 600.0, 30.0, 30), True),
    # Splits only when the European bound is applied to this American put.
    ("put_warrant_outlives",  warrant("W5", "Put", 605.0, 5.0, 365, 0.5),
                              option("P600", "Put", 600.0, 12.0, 30), False),
    ("call_ratio_0_05",       warrant("W6", "Call", 580.0, 1.2, 30, 0.05),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("call_thin_edge",        warrant("W7", "Call", 580.0, 14.975, 30, 0.5),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("call_min_depth",        warrant("W8", "Call", 580.0, 12.0, 30, 0.5, ask_qty=4),
                              option("C600", "Call", 600.0, 30.0, 30, bid_size=1), True),
    ("call_ratio_1",          warrant("W9", "Call", 580.0, 24.0, 30, 1.0),
                              option("C600", "Call", 600.0, 30.0, 30), True),
    ("call_deep_itm",         warrant("W10", "Call", 400.0, 55.0, 30, 0.5),
                              option("C500", "Call", 500.0, 115.0, 30), True),
]


def direct_rows(w, o):
    rows = arb_logic._match_warrants_to_options(
        pd.DataFrame([w]), pd.DataFrame([o]), M,
        max_strike_diff_pct=100.0, max_dte_diff=365, positive_loose=False)
    return [r for r in rows if r["trade"] == "Buy Warrant / Sell Option"]


def lp_row(w, o, american_put_floor=None):
    """The LP's core at the option's expiry — the only horizon a single option offers.

    `american_put_floor` overrides the module constant for the call, so both
    settings can be exercised without depending on which one ships.
    """
    prev = static_arb.AMERICAN_PUT_INTRINSIC_FLOOR
    if american_put_floor is not None:
        static_arb.AMERICAN_PUT_INTRINSIC_FLOOR = american_put_floor
    try:
        T = int(o["days_to_expiry"])
        longs, shorts, _ = static_arb._build_legs(
            pd.DataFrame([w]), pd.DataFrame([o]), T, M, R)
        return static_arb._solve_horizon(longs, shorts, T, min_edge=0.0)
    finally:
        static_arb.AMERICAN_PUT_INTRINSIC_FLOOR = prev


@pytest.mark.parametrize("name,w,o,_x", CASES, ids=[c[0] for c in CASES])
def test_every_case_is_an_arb_for_direct_match(name, w, o, _x):
    """All ten are buy-warrant / sell-option arbs by Direct Match's own rule."""
    rows = direct_rows(w, o)
    assert len(rows) == 1, name
    assert rows[0]["price_diff"] > 0
    assert rows[0]["riskless"] is True


@pytest.mark.parametrize("name,w,o,_x", CASES, ids=[c[0] for c in CASES])
def test_lp_captures_every_case_with_the_american_floor(name, w, o, _x):
    """With the American exercise floor on, the LP reproduces all ten."""
    assert lp_row(w, o, american_put_floor=True) is not None, name


@pytest.mark.parametrize("name,w,o,fires_european", CASES, ids=[c[0] for c in CASES])
def test_european_floor_costs_the_lp_the_outliving_put(name, w, o, fires_european):
    """With the European bound instead, the one put whose warrant outlives the
    option drops out — the bound understates a claim you could exercise."""
    assert (lp_row(w, o, american_put_floor=False) is not None) is fires_european, name


def test_the_flag_only_moves_puts():
    """A call's long strike is discounted downward, which only lifts the payoff,
    so no call case can depend on the setting however long the warrant runs."""
    for w_dte in (30, 90, 365, 1000):
        w = warrant("Wc", "Call", 580.0, 12.0, w_dte, 0.5)
        o = option("Cc", "Call", 600.0, 30.0, 30)
        assert direct_rows(w, o)
        assert lp_row(w, o, american_put_floor=False) is not None
        assert lp_row(w, o, american_put_floor=True) is not None


@pytest.mark.parametrize("outlives_days", [30, 90, 180, 365])
def test_same_strike_puts_need_the_american_floor(outlives_days):
    """The shape that matters in a real chain: identical strikes, warrant merely
    longer-dated. There is no cushion at all, so the European bound always
    rejects it and the American floor always accepts it."""
    w = warrant("Wp", "Put", 600.0, 5.0, 30 + outlives_days, 0.5)
    o = option("Pp", "Put", 600.0, 12.0, 30)
    assert direct_rows(w, o)
    assert lp_row(w, o, american_put_floor=False) is None
    assert lp_row(w, o, american_put_floor=True) is not None


@pytest.mark.parametrize("outlives_days", [30, 60, 90, 180, 365])
def test_european_bound_threshold_is_the_discount(outlives_days):
    """Under the European bound the LP accepts a put pair exactly while
    Kw*exp(-r*tau) >= Ko. Straddling that boundary flips the LP and leaves
    Direct unchanged — which is what identifies the discount as the cause."""
    Ko, o_dte = 600.0, 30
    w_dte = o_dte + outlives_days
    needed = np.exp(R * outlives_days / 365.0)          # Kw/Ko ratio required
    # Below the boundary, stay ABOVE 1.0 — Direct needs Ko <= Kw to fire at all,
    # and for a short outlive the required cushion is only ~0.15%.
    below = 1.0 + (needed - 1.0) * 0.5

    for factor, lp_expected in ((needed * 1.002, True), (below, False)):
        w = warrant("Wx", "Put", round(Ko * factor, 4), 5.0, w_dte, 0.5)
        o = option("Px", "Put", Ko, 12.0, o_dte)
        assert direct_rows(w, o), "Direct should fire on both sides of the boundary"
        assert (lp_row(w, o, american_put_floor=False) is not None) is lp_expected, (
            f"outlives={outlives_days}d cushion_factor={factor:.6f}")


