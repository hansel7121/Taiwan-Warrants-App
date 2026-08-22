"""The Rust arb kernels must return exactly what the Python references return.

Same bar as the IV engine: identical rows after `round(..., 4)`, identical
None/NaN placement, identical order. The Python half runs unguarded so a machine
without a toolchain still exercises the fallback; only the Rust half is skipped.
"""
import numpy as np
import pytest

from logic import arb_kernels_py, arb_logic, iv_engine
from tests.conftest import rust_only
from tests.logic import arb_golden

BUTTERFLY = [n for n in arb_golden.scenarios() if n.startswith("butterfly")]


@rust_only
@pytest.mark.parametrize("name", BUTTERFLY)
def test_butterfly_matcher_identical_across_engines(monkeypatch, name):
    frames, params, expected = arb_golden.load(name)
    rust_rows = arb_golden.run(name, frames, params)

    monkeypatch.setattr(arb_logic, "butterfly_pairs", arb_kernels_py.butterfly_pairs)
    py_rows = arb_golden.run(name, frames, params)

    arb_golden.assert_rows_match(rust_rows, py_rows, name)
    arb_golden.assert_order_match(rust_rows, py_rows, name)


@rust_only
def test_butterfly_kernel_agrees_on_random_chains():
    """Seeded random chains, to reach the branches the recorded fixtures miss —
    ties on the body's sell price, wings that share a strike, empty ranges."""
    rng = np.random.default_rng(4)
    for trial in range(60):
        nw = int(rng.integers(2, 25))
        nb = int(rng.integers(1, 30))
        wing_k = np.sort(rng.choice(np.arange(50, 120, 2.5), nw, replace=False))
        wing_buy = np.round(rng.uniform(0.05, 6.0, nw), 6)
        wing_dte = rng.integers(5, 200, nw)
        order = np.argsort(rng.choice(np.arange(50, 120, 2.5), nb))
        body_k = np.sort(rng.choice(np.arange(50, 120, 2.5), nb))
        # A coarse price grid makes exact ties on sell_ps common, which is where
        # the tie-break (first in chain order) has to agree.
        body_sell = np.round(rng.choice([0.5, 1.0, 1.5, 2.0, 4.0], nb), 6)
        body_dte = rng.integers(5, 200, nb)
        body_orig = order.astype(np.int64)
        for is_call in (True, False):
            args = (wing_k, wing_buy, wing_dte, body_k, body_sell, body_dte,
                    body_orig, is_call)
            got = iv_engine.butterfly_pairs(*args)
            want = arb_kernels_py.butterfly_pairs(*args)
            assert len(got) == len(want), f"trial {trial} is_call={is_call}"
            for g, w in zip(got, want):
                assert g[:3] == tuple(int(v) for v in w[:3])
                for gi, wi in zip(g[3:], w[3:]):
                    assert gi == pytest.approx(wi, abs=0, rel=0)


DIRECT = [n for n in arb_golden.scenarios()
          if n.startswith("direct") or n.startswith("us_direct")]


@rust_only
@pytest.mark.parametrize("name", DIRECT)
def test_direct_matcher_identical_across_engines(monkeypatch, name):
    frames, params, expected = arb_golden.load(name)
    rust_rows = arb_golden.run(name, frames, params)

    monkeypatch.setattr(arb_logic, "direct_pairs", arb_kernels_py.direct_pairs)
    py_rows = arb_golden.run(name, frames, params)

    arb_golden.assert_rows_match(rust_rows, py_rows, name)
    arb_golden.assert_order_match(rust_rows, py_rows, name)


@rust_only
def test_direct_kernel_agrees_on_random_chains():
    """Seeded random chains, to reach what the fixtures miss — one-sided books,
    NaN quotes behind a live flag, zero and negative exercise ratios, and both
    price branches under each positive_loose setting."""
    rng = np.random.default_rng(9)
    for trial in range(40):
        nw = int(rng.integers(1, 30))
        no = int(rng.integers(1, 20))
        w_type = rng.integers(0, 2, nw).tolist()
        w_is_put = [t == 1 for t in w_type]
        w_strike = np.round(rng.uniform(40, 120, nw), 2).tolist()
        w_dte = rng.integers(1, 200, nw).tolist()
        # zeros and negatives must drop the warrant, not divide by them
        w_ratio = rng.choice([0.0, -1.0, 0.05, 0.5, 1.0], nw).tolist()
        w_ask = np.round(rng.uniform(0.0, 5.0, nw), 2).tolist()
        w_bid = [float("nan") if rng.random() < 0.2 else round(a * 0.9, 2)
                 for a in w_ask]
        o_type = rng.integers(0, 2, no).tolist()
        o_strike = np.round(rng.uniform(40, 120, no), 2).tolist()
        o_dte = rng.integers(1, 200, no).tolist()
        o_bid = np.round(rng.uniform(0.0, 6.0, no), 2).tolist()
        o_ask = [b + 0.05 for b in o_bid]
        o_bid_live = (rng.random(no) < 0.7).tolist()
        o_ask_live = (rng.random(no) < 0.7).tolist()
        # a live flag over a NaN quote is a real state and must behave the same
        for j in range(no):
            if rng.random() < 0.1:
                o_bid[j] = float("nan")
        for loose in (False, True):
            args = (w_type, w_is_put, w_strike, w_dte, w_ratio, w_ask, w_bid,
                    o_type, o_strike, o_dte, o_bid, o_ask, o_bid_live, o_ask_live,
                    int(rng.integers(0, 30)), loose)
            got = iv_engine.direct_pairs(*args)
            want = arb_kernels_py.direct_pairs(*args)
            assert len(got) == len(want), f"trial {trial} loose={loose}"
            for g, w in zip(got, want):
                assert tuple(g) == tuple(w) or all(
                    (a == b) or (a != a and b != b) for a, b in zip(g, w)
                ), f"trial {trial} loose={loose}: {g} != {w}"


@rust_only
@pytest.mark.parametrize("nd", [0, 2, 4, 6])
def test_rust_round_matches_python_round(nd):
    """The kernels round with Python's builtin `round`, not NumPy's — the two
    disagree on ties, and the butterfly's `guaranteed_ps > 0` test rides on it."""
    import warrants_core

    rng = np.random.default_rng(11)
    xs = rng.uniform(-1e4, 1e4, 20000) * 10.0 ** rng.integers(-8, 4, 20000)
    for x in xs:
        assert warrants_core.round_py(float(x), nd) == round(float(x), nd)
    for tie in (2.675, 0.125, 1.005, 8.835, -2.675, 0.5, 2.5, 1.0000005):
        assert warrants_core.round_py(tie, nd) == round(tie, nd)
