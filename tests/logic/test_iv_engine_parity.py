"""Parity between the Rust IV engine and the pure-Python reference.

Guards the swap in logic/iv_engine.py: every column the app displays is rounded
to 4 dp, so the contract is that both engines agree exactly after round(..., 4)
— including NaN placement — for scalars, the vectorized paths, and the whole
build_warrant_df frame.
"""
import numpy as np
import pytest

from logic import bs_python, iv_engine, warrant_logic

rust_only = pytest.mark.skipif(
    not iv_engine.RUST_AVAILABLE,
    reason=f"warrants_core not built ({iv_engine.RUST_IMPORT_ERROR})",
)


def _sample(n, seed=7):
    """Scanner-shaped inputs: near-the-money strikes, warrant-style exercise ratios, plus degenerate rows."""
    rng = np.random.default_rng(seed)
    S = rng.uniform(15.0, 1400.0, n)
    K = S * rng.uniform(0.55, 1.75, n)
    T = rng.uniform(1.0, 500.0, n) / 365.0
    r = np.full(n, 0.02)
    ratio = rng.choice([0.05, 0.1, 0.25, 0.5, 1.0, 2.0], n)
    is_put = rng.random(n) < 0.5
    sigma = rng.uniform(0.05, 2.0, n)
    price = np.array([
        bs_python.bs_price(S[i], K[i], T[i], r[i], sigma[i], ratio[i], bool(is_put[i]))
        for i in range(n)
    ])
    # Degenerate rows the solver must reject identically in both engines.
    price[:: 37] = 0.0                       # no quote
    price[1:: 53] = np.nan                   # missing quote
    T[2:: 61] = 0.0                          # expired
    price[3:: 71] = 1e-9                     # below intrinsic / unsolvable
    return price, S, K, T, r, ratio, is_put


def _same(a, b):
    """Equal after round(,4), with NaNs in the same places."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    assert (np.isnan(a) == np.isnan(b)).all(), "NaN placement differs"
    m = ~np.isnan(a)
    assert (np.round(a[m], 4) == np.round(b[m], 4)).all()


@rust_only
def test_implied_vol_vec_matches_python():
    price, S, K, T, r, ratio, is_put = _sample(30000)
    _same(
        iv_engine.implied_vol_vec(price, S, K, T, r, ratio, is_put),
        bs_python.implied_vol_vec(price, S, K, T, r, ratio, is_put),
    )


@rust_only
def test_implied_vol_scalar_matches_python():
    price, S, K, T, r, ratio, is_put = _sample(800, seed=11)
    for i in range(len(price)):
        a = iv_engine.implied_vol(price[i], S[i], K[i], T[i], r[i], ratio[i], bool(is_put[i]))
        b = bs_python.implied_vol(price[i], S[i], K[i], T[i], r[i], ratio[i], bool(is_put[i]))
        assert np.isnan(a) == np.isnan(b)
        if not np.isnan(a):
            assert abs(a - b) < 1e-9


@rust_only
def test_bs_price_delta_vega_scalar_match():
    price, S, K, T, r, ratio, is_put = _sample(500, seed=13)
    rng = np.random.default_rng(3)
    sig = rng.uniform(0.05, 2.0, len(S))
    for i in range(len(S)):
        args = (S[i], K[i], T[i], r[i], sig[i], ratio[i])
        assert iv_engine.bs_price(*args, bool(is_put[i])) == pytest.approx(
            bs_python.bs_price(*args, bool(is_put[i])), rel=1e-12, abs=1e-12)
        assert iv_engine.bs_delta(*args, bool(is_put[i])) == pytest.approx(
            bs_python.bs_delta(*args, bool(is_put[i])), rel=1e-12, abs=1e-12)
        assert iv_engine.bs_vega(*args) == pytest.approx(
            bs_python.bs_vega(*args), rel=1e-12, abs=1e-12)


@rust_only
def test_bs_delta_vec_matches_python():
    price, S, K, T, r, ratio, is_put = _sample(20000, seed=5)
    iv = bs_python.implied_vol_vec(price, S, K, T, r, ratio, is_put)
    _same(
        iv_engine.bs_delta_vec(S, K, T, r, iv, ratio, is_put),
        bs_python.bs_delta_vec(S, K, T, r, iv, ratio, is_put),
    )


@rust_only
@pytest.mark.parametrize("check_delta,check_leverage", [(False, False), (True, False), (True, True)])
def test_refine_iv_for_rounding_matches_python(check_delta, check_leverage):
    price, S, K, T, r, ratio, is_put = _sample(20000, seed=17)
    iv = bs_python.implied_vol_vec(price, S, K, T, r, ratio, is_put)
    kw = dict(check_delta=check_delta, check_leverage=check_leverage, lev_price=price)
    _same(
        iv_engine._refine_iv_for_rounding(iv, price, S, K, T, r, ratio, is_put, **kw),
        bs_python._refine_iv_for_rounding(iv, price, S, K, T, r, ratio, is_put, **kw),
    )


@rust_only
def test_scalar_and_vector_engines_agree():
    """The vectorized fast path must round like the exact per-row brentq solve."""
    price, S, K, T, r, ratio, is_put = _sample(20000, seed=23)
    vec = iv_engine.implied_vol_vec(price, S, K, T, r, ratio, is_put)
    exact = np.array([
        bs_python.implied_vol(price[i], S[i], K[i], T[i], r[i], ratio[i], bool(is_put[i]))
        for i in range(len(price))
    ])
    _same(vec, exact)


def _fake_cmoney(n=600, seed=29):
    """CMoney-shaped payloads for build_warrant_df, including one-sided books."""
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n):
        S = float(rng.uniform(20, 900))
        K = float(S * rng.uniform(0.6, 1.6))
        dte = int(rng.integers(1, 400))
        ratio = float(rng.choice([0.05, 0.1, 0.25, 0.5, 1.0]))
        is_put = bool(rng.random() < 0.5)
        sigma = float(rng.uniform(0.1, 1.4))
        ask = bs_python.bs_price(S, K, dte / 365.0, 0.02, sigma, ratio, is_put)
        bid = ask * float(rng.uniform(0.85, 0.999))
        if i % 23 == 0:
            bid = 0.0            # no bid
        if i % 47 == 0:
            ask = 0.0            # no ask
        out[f"0{i:05d}"] = {
            "Warrant": {
                "SellPr1": round(ask, 2), "BuyPr1": round(bid, 2),
                "SellQty1": 10, "BuyQty1": 8, "SaleQty": int(rng.integers(0, 5000)),
                "CommName": f"W{i}", "LastDays": dte, "StrikePr": K,
                "UserRate": ratio, "CallorPut": 2 if is_put else 1,
            },
            "Stock": {"CommKey": "2330", "SalePr": S},
        }
    return out


@rust_only
@pytest.mark.parametrize("keep_noniv,allow_no_quote", [(False, False), (True, True)])
def test_build_warrant_df_identical_across_engines(monkeypatch, keep_noniv, allow_no_quote):
    """The Rust frame builder against the pure-Python one, end to end.

    Dtypes are asserted alongside values: the two engines reach the frame by
    different routes (column arrays vs a list of dicts), `to_json` renders both
    NaN and None as null, and a column that silently became object-dtype would
    pass every value check while breaking `.fillna`, `> 0` comparisons and the
    Supabase writer.
    """
    from logic import warrant_frame_py

    data = _fake_cmoney()
    rust_df = warrant_logic.build_warrant_df(
        data, keep_noniv=keep_noniv, allow_no_quote=allow_no_quote)

    for name in ("implied_vol", "implied_vol_vec", "bs_delta_vec", "_refine_iv_for_rounding"):
        monkeypatch.setattr(warrant_frame_py, name, getattr(bs_python, name))
    py_df = warrant_frame_py.build_warrant_df(
        data, keep_noniv=keep_noniv, allow_no_quote=allow_no_quote)

    assert list(rust_df.columns) == list(py_df.columns)
    assert len(rust_df) == len(py_df)
    assert rust_df["warrant_code"].tolist() == py_df["warrant_code"].tolist()
    assert rust_df.dtypes.equals(py_df.dtypes), (
        f"dtype drift:\n{rust_df.dtypes}\nvs\n{py_df.dtypes}")
    assert (rust_df.isna().to_numpy() == py_df.isna().to_numpy()).all()
    for col in rust_df.columns:
        if rust_df[col].dtype == object:
            assert rust_df[col].tolist() == py_df[col].tolist(), col
        else:
            _same(rust_df[col].to_numpy(dtype=float), py_df[col].to_numpy(dtype=float))


@rust_only
def test_empty_result_frames_match():
    """An all-dropped batch must give the same empty frame from either engine."""
    from logic import warrant_frame_py

    bad = {"030000": {"Warrant": {}, "Stock": {}}}
    rust_df = warrant_logic.build_warrant_df(bad)
    py_df = warrant_frame_py.build_warrant_df(bad)
    assert rust_df.empty and py_df.empty
    assert list(rust_df.columns) == list(py_df.columns)
