"""StatArb structure finder (logic/statarb_lp.py): whole-lot MILP with P&L >= 0 on a spot band holding 1 - cap
of every path set, max-loss budget, out-of-sample P(loss)."""
import numpy as np

from logic import statarb_lp as lp


def _leg(kind, code, typ, strike, price, depth, lot_shares):
    return {"kind": kind, "code": code, "name": code, "type": typ, "is_call": typ == "Call", "strike": strike,
            "eff_strike": strike, "dte": 30, "ratio": None if kind == "option" else lot_shares / 1000,
            "quote": price, "price_ps": price, "depth_lots": depth, "depth_shares": depth * lot_shares,
            "lot_shares": lot_shares, "disc": 1.0}


def _sets(seed=0, n=20000):
    end = 1000 * np.exp(np.random.default_rng(seed).normal(0, 0.05, n))
    return {"zero": end[: n // 2]}, {"zero": end[n // 2:]}


def test_riskless_vertical_comes_back_pure():
    """Long a 1000 call for 10, short a 1050 call for 20: credit with no loss anywhere."""
    longs = [_leg("option", "L1000", "Call", 1000.0, 10.0, 5, 2000.0)]
    shorts = [_leg("option", "S1050", "Call", 1050.0, 20.0, 5, 2000.0)]
    fit, check = _sets()
    row = lp.solve_horizon(longs, shorts, fit, check, cap=0.05, max_loss=1e6)
    assert row["pure"] and row["passes"] and row["max_loss"] == 0
    assert row["net_credit"] == 5 * 2000 * 10


def test_far_otm_short_put_passes_and_respects_cap_out_of_sample():
    """A naked short put far below spot passes under the cap; its band edge sits below the strike."""
    shorts = [_leg("option", "P800", "Put", 800.0, 0.5, 3, 2000.0)]
    longs = [_leg("warrant", "W500", "Put", 500.0, 0.01, 10_000, 100.0)]   # cheap far tail cover
    fit, check = _sets()
    row = lp.solve_horizon(longs, shorts, fit, check, cap=0.05, max_loss=5e6)
    assert row is not None and row["passes"] and not row["pure"]
    assert max(row["p_expiry"].values()) <= 0.05
    assert all(isinstance(l["lots"], int) and l["lots"] >= 1 for l in row["legs"])


def test_max_loss_budget_blocks_naked_short():
    """The same short put risks 800 x 2000 per contract, so a 100k budget forces cover or rejects it."""
    shorts = [_leg("option", "P800", "Put", 800.0, 0.5, 3, 2000.0)]
    fit, check = _sets()
    assert lp.solve_horizon([_leg("option", "dummy", "Call", 5000.0, 9.0, 1, 2000.0)], shorts, fit, check,
                            cap=0.05, max_loss=1e5) is None


def test_near_the_money_short_fails_the_cap():
    """A short put at spot loses on about half the paths: no band of 95% can contain only its profit side."""
    shorts = [_leg("option", "P1000", "Put", 1000.0, 5.0, 3, 2000.0)]
    fit, check = _sets()
    assert lp.solve_horizon([_leg("option", "dummy", "Call", 5000.0, 9.0, 1, 2000.0)], shorts, fit, check,
                            cap=0.05, max_loss=1e7) is None
