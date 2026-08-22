"""Malformed CMoney payloads must drop exactly the rows they always dropped.

`build_warrant_df`'s parse loop caught a bare `Exception`, which has no
equivalent in the Rust engine. Narrowing it to named exceptions is only safe if
no payload's kept/dropped status moves, so every shape the fetcher can hand over
— missing keys, nulls, strings where numbers belong, zero and negative values —
is asserted here against the surviving row set.
"""
import numpy as np
import pandas as pd
import pytest

from logic import warrant_logic


def payload(**over):
    w = {"SellPr1": 2.1, "BuyPr1": 2.0, "SellQty1": 10, "BuyQty1": 8,
         "SaleQty": 100, "CommName": "W", "LastDays": 90, "StrikePr": 600.0,
         "UserRate": 0.5, "CallorPut": 1}
    s = {"CommKey": "2330", "SalePr": 612.0}
    w.update(over.pop("warrant", {}))
    s.update(over.pop("stock", {}))
    return {"Warrant": w, "Stock": s}


BROKEN = {
    "missing_warrant_key": {"Stock": {"CommKey": "2330", "SalePr": 612.0}},
    "missing_stock_key": {"Warrant": payload()["Warrant"]},
    "warrant_is_none": {"Warrant": None, "Stock": {"CommKey": "2330", "SalePr": 1.0}},
    "stock_is_a_list": {"Warrant": payload()["Warrant"], "Stock": []},
    "strike_is_text": payload(warrant={"StrikePr": "not-a-number"}),
    "dte_is_text": payload(warrant={"LastDays": "soon"}),
    "qty_is_text": payload(warrant={"SellQty1": "many"}),
    "spot_is_text": payload(stock={"SalePr": "n/a"}),
    "ratio_is_none": payload(warrant={"UserRate": None}),
    "everything_null": {"Warrant": {}, "Stock": {}},
    # An exercise ratio of 0 divides by zero computing time value. It has always
    # been dropped here rather than by an explicit guard.
    "zero_ratio": payload(warrant={"UserRate": 0}),
}

VALID = {
    "plain": payload(),
    "put": payload(warrant={"CallorPut": 2, "StrikePr": 650.0}),
    "no_bid": payload(warrant={"BuyPr1": 0}),
    "null_commkey": payload(stock={"CommKey": None}),
    "negative_strike_gap": payload(warrant={"StrikePr": 400.0}),
}

DROPPED_BY_FILTER = {
    "expired": payload(warrant={"LastDays": 0}),
    "no_spot": payload(stock={"SalePr": 0}),
}


def _codes(data, **kw):
    df = warrant_logic.build_warrant_df(data, keep_noniv=True, allow_no_quote=True, **kw)
    return set(df["warrant_code"]) if not df.empty else set()


def test_malformed_payloads_are_dropped_not_raised():
    data = {f"0{i:05d}": p for i, p in enumerate(BROKEN.values())}
    assert _codes(data) == set()


def test_valid_payloads_survive_alongside_malformed_ones():
    data = {}
    good = []
    for i, (name, p) in enumerate(list(VALID.items()) + list(BROKEN.items())):
        code = f"0{i:05d}"
        data[code] = p
        if name in VALID:
            good.append(code)
    assert _codes(data) == set(good)


def test_filter_drops_are_separate_from_parse_drops():
    """Expired / no-spot rows are dropped by an explicit guard, not by the except."""
    data = {f"0{i:05d}": p for i, p in enumerate(DROPPED_BY_FILTER.values())}
    assert _codes(data) == set()


def test_no_ask_row_needs_allow_no_quote():
    data = {"030000": payload(warrant={"SellPr1": 0})}
    assert warrant_logic.build_warrant_df(data, allow_no_quote=False).empty
    assert not warrant_logic.build_warrant_df(
        data, keep_noniv=True, allow_no_quote=True).empty


def test_columns_and_dtypes_are_stable():
    data = {f"0{i:05d}": p for i, p in enumerate(VALID.values())}
    df = warrant_logic.build_warrant_df(data, keep_noniv=True, allow_no_quote=True)
    assert list(df.columns) == warrant_logic.COL_ORDER
    assert df["days_to_expiry"].dtype == np.int64
    assert df["underlying_price"].dtype == np.float64
    assert df["warrant_code"].dtype == object
