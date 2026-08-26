"""Parity + correctness for the Live Warrant tab's per-tick time-value kernel.

`solve_tick` deliberately computes only time_value/bid_time_value_pct/
ask_time_value_pct — no IV, no delta, no leverage, not even internally (see
logic/bs_python.solve_tick and rust/warrants_core/src/tick.rs). This file
proves: (1) the Rust and Python engines agree, and (2) the Python engine
agrees with the already-trusted whole-frame builder (`build_warrant_df`) on
the same time-value numbers, so the new scalar kernel isn't just internally
consistent but actually correct.
"""
import numpy as np
import pytest

from logic import bs_python, iv_engine

rust_only = pytest.mark.skipif(
    not iv_engine.RUST_AVAILABLE,
    reason=f"warrants_core not built ({iv_engine.RUST_IMPORT_ERROR})",
)


def _sample(n, seed=41):
    """Warrant-shaped inputs, including no-bid / no-ask / put and call rows."""
    rng = np.random.default_rng(seed)
    S = rng.uniform(15.0, 1400.0, n)
    K = S * rng.uniform(0.55, 1.75, n)
    ratio = rng.choice([0.05, 0.1, 0.25, 0.5, 1.0, 2.0], n)
    is_put = rng.random(n) < 0.5
    ask = rng.uniform(0.01, 20.0, n)
    bid = ask * rng.uniform(0.85, 0.999, n)
    ask[:: 17] = 0.0   # no ask
    bid[1:: 23] = 0.0  # no bid
    return S, K, ratio, is_put, bid, ask


def _same_scalar(a, b):
    a_nan, b_nan = np.isnan(a), np.isnan(b)
    assert a_nan == b_nan, f"NaN placement differs: {a} vs {b}"
    if not a_nan:
        assert round(a, 4) == round(b, 4), f"{a} != {b}"


@rust_only
def test_solve_tick_rust_matches_python():
    S, K, ratio, is_put, bid, ask = _sample(4000)
    for i in range(len(S)):
        rust = iv_engine.solve_tick(S[i], K[i], ratio[i], bool(is_put[i]), bid[i], ask[i])
        py = bs_python.solve_tick(S[i], K[i], ratio[i], bool(is_put[i]), bid[i], ask[i])
        for a, b in zip(rust, py):
            _same_scalar(float(a), float(b))


def test_solve_tick_zero_ratio_degrades_to_nan():
    """A live tick must never raise on a bad ratio (unlike the batch path, which drops the row)."""
    tv, bid_pct, ask_pct = bs_python.solve_tick(100.0, 95.0, 0.0, False, 4.0, 5.0)
    assert np.isnan(tv) and np.isnan(bid_pct) and np.isnan(ask_pct)


def test_solve_tick_matches_build_warrant_df():
    """The new scalar kernel must agree with the already-trusted whole-frame
    builder's time_value / *_time_value_pct numbers for an equivalent row."""
    from logic import warrant_frame_py

    cases = [
        # (S, K, ratio, is_put, bid, ask)
        (620.0, 590.0, 0.1, False, 3.2, 3.4),   # ITM call
        (620.0, 650.0, 0.25, False, 0.5, 0.6),  # OTM call
        (150.0, 160.0, 0.5, True, 2.1, 2.3),    # ITM put
        (150.0, 130.0, 1.0, True, 0.1, 0.15),   # OTM put
    ]
    for i, (S, K, ratio, is_put, bid, ask) in enumerate(cases):
        code = f"0{i:05d}"
        cmoney = {
            code: {
                "Warrant": {
                    "SellPr1": ask, "BuyPr1": bid, "SellQty1": 10, "BuyQty1": 8,
                    "SaleQty": 100, "CommName": f"W{i}", "LastDays": 90,
                    "StrikePr": K, "UserRate": ratio, "CallorPut": 2 if is_put else 1,
                },
                "Stock": {"CommKey": "2330", "SalePr": S},
            }
        }
        df = warrant_frame_py.build_warrant_df(cmoney, compute_iv=False, keep_noniv=True)
        row = df.iloc[0]

        tv, bid_pct, ask_pct = bs_python.solve_tick(S, K, ratio, is_put, bid, ask)
        _same_scalar(tv, float(row["time_value"]))
        _same_scalar(bid_pct, float(row["bid_time_value_pct"]))
        _same_scalar(ask_pct, float(row["ask_time_value_pct"]))
