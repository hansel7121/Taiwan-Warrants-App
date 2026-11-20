"""Direct Match must ignore non-positive book prices.

The Fubon feed reports a resting market order (nothing on the other side to
fill against) as price 0. On 2026-11-10 a market sell on warrant 056776 sat at
"ask 0" for 3.4 h and the EOD replay logged 704 bogus Direct Match episodes
against every dominating option. Shape below mirrors that day.
"""
from datetime import date

import pytest

from logic import arb_kernels_py, live_arb_logic

TODAY = date(2026, 11, 10)


def _warrant(ask, bid=None):
    return {"code": "056776", "name": "台積電元大61購10", "type": "Call", "strike": 1983.22,
            "exercise_ratio": 0.004, "maturity": date(2027, 1, 22),
            "best": {"bid": bid, "ask": ask, "bid_size": None if bid is None else 10,
                     "ask_size": None if ask is None else 10}}


def _option(bid, ask=None):
    return {"code": "CDO20000L6", "name": "台積電 2000 Call 202612", "type": "Call", "strike": 2000.0,
            "expiry": date(2026, 12, 16),
            "best": {"bid": bid, "ask": ask, "bid_size": None if bid is None else 1,
                     "ask_size": None if ask is None else 1}}


@pytest.mark.parametrize("pairs_fn", [None, arb_kernels_py.direct_pairs])
@pytest.mark.parametrize("ask", [0, 0.0, -1.0])
def test_zero_or_negative_warrant_ask_is_not_a_price(pairs_fn, ask):
    assert live_arb_logic.scan([_warrant(ask)], [_option(239.0)], TODAY, pairs_fn=pairs_fn) == []


@pytest.mark.parametrize("pairs_fn", [None, arb_kernels_py.direct_pairs])
def test_positive_cheap_ask_still_matches(pairs_fn):
    # 0.5 / 0.004 = 125 per share < 239 option bid: a genuine Direct Match.
    hits = live_arb_logic.scan([_warrant(0.5)], [_option(239.0)], TODAY, pairs_fn=pairs_fn)
    assert len(hits) == 1


def test_build_direct_arrays_blanks_non_positive_quotes():
    w, o = live_arb_logic.build_direct_arrays([_warrant(0, bid=0)], [_option(0, ask=-2)], TODAY)
    assert all(v != v for v in (w[0]["ask"], w[0]["bid"], o[0]["bid"], o[0]["ask"]))  # NaN
    assert o[0]["bid_live"] is False and o[0]["ask_live"] is False
