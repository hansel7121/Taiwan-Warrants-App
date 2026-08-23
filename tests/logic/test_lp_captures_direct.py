"""Does the static-arb LP capture every arb Direct Match reports?

The LP's docstring claims completeness over static buy-and-hold portfolios, so
Direct Match's vertical should fall out as a weight-restricted special case.
Ten single-warrant / single-option pairs test that, each a clear buy-warrant /
sell-option arb.

It holds for calls and fails for puts, and where it fails **Direct is the unsound
one**: the LP floors a long leg that outlives the horizon at its European lower
bound, `(K*exp(-r*tau) - S)+`, while Direct compares raw strikes. For a call the
discount lowers the long strike and only lifts the payoff, so the two agree. For
a put it lowers the payoff, and Direct certifies `riskless=True,
max_loss_per_share=0.0` on structures that can lose at expiry.

The threshold is exact: the LP accepts a put pair only while
`Kw*exp(-r*tau) >= Ko`, i.e. while the warrant's strike cushion covers the
discount over the days it outlives the option — 0.15% for 30 days, 0.93% for 180.
A same-strike put pair with the warrant merely longer-dated never clears it.
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
    # The one divergence: 335 days of discount against a 0.83% strike cushion.
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


def lp_row(w, o):
    """The LP's core at the option's expiry — the only horizon a single option offers."""
    T = int(o["days_to_expiry"])
    longs, shorts, _ = static_arb._build_legs(
        pd.DataFrame([w]), pd.DataFrame([o]), T, M, R, allow_short_warrants=False)
    return static_arb._solve_horizon(longs, shorts, T, min_edge=0.0)


@pytest.mark.parametrize("name,w,o,_lp", CASES, ids=[c[0] for c in CASES])
def test_every_case_is_an_arb_for_direct_match(name, w, o, _lp):
    """All ten are buy-warrant / sell-option arbs by Direct Match's own rule."""
    rows = direct_rows(w, o)
    assert len(rows) == 1, name
    assert rows[0]["price_diff"] > 0
    assert rows[0]["riskless"] is True


@pytest.mark.parametrize("name,w,o,lp_should_fire", CASES, ids=[c[0] for c in CASES])
def test_lp_agrees_except_on_the_discounted_put(name, w, o, lp_should_fire):
    row = lp_row(w, o)
    assert (row is not None) is lp_should_fire, (
        f"{name}: LP {'missed' if lp_should_fire else 'accepted'} what Direct reports")


def test_the_divergence_is_direct_being_unsound_not_the_lp():
    """Direct calls put_warrant_outlives riskless with zero max loss. It isn't:
    at spot 0 the short put pays the full strike while the long warrant is only
    provably worth its discounted strike, and the gap exceeds the entry credit."""
    _, w, o, _ = CASES[4]
    row = direct_rows(w, o)[0]
    assert row["riskless"] is True and row["max_loss_per_share"] == 0.0

    tau = (w["days_to_expiry"] - o["days_to_expiry"]) / 365.0
    long_floor_at_zero = w["strike"] * np.exp(-R * tau)
    worst = long_floor_at_zero - o["strike"]
    assert worst < 0, "the long put's discounted strike should fall below the short's"
    assert worst + row["price_diff"] < 0, "the entry credit should not cover it"


@pytest.mark.parametrize("outlives_days", [30, 60, 90, 180, 365])
def test_put_cushion_threshold_is_the_discount(outlives_days):
    """A put pair clears the LP exactly while Kw*exp(-r*tau) >= Ko. Straddling
    that boundary by a hair flips the LP and leaves Direct unchanged."""
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
        assert (lp_row(w, o) is not None) is lp_expected, (
            f"outlives={outlives_days}d cushion_factor={factor:.6f}")


@pytest.mark.parametrize("w_dte", [30, 90, 365, 1000])
def test_calls_never_diverge_however_long_the_warrant_runs(w_dte):
    """For a call the discount lowers the long strike, which only lifts the
    payoff — so no amount of extra warrant life can split the two engines."""
    w = warrant("Wc", "Call", 580.0, 12.0, w_dte, 0.5)
    o = option("Cc", "Call", 600.0, 30.0, 30)
    assert direct_rows(w, o)
    assert lp_row(w, o) is not None
