"""Tick -> warrant-row translation.

Pure in-memory: a hand-built snapshot row in `warrant_logic.COL_ORDER` shape and
a hand-built Tick, with no broker and no Supabase. The last test drives the
merged row through `arb_logic.check_tick` to prove the shape is the one the
matchers actually consume.
"""
from datetime import datetime

import pandas as pd
import pytest

from logic import arb_logic
from services.broker.base import Tick
from services.broker.tick_translate import TickTranslationError, tick_to_warrant_row

CODE = "2330"
WARRANT = "031101"
CONTRACT_SIZE = 2000
SPOT = 600.0
TS = datetime(2026, 8, 3, 10, 30)


def _snapshot(**over):
    row = dict(
        warrant_code=WARRANT, warrant_name="W-550", underlying_code=CODE,
        type="Call", underlying_price=SPOT, ask=5.0, bid=4.9,
        ask_qty=100, bid_qty=100, days_to_expiry=60, strike=550.0,
        exercise_ratio=0.5, volume=1000, time_value=1.0, time_value_pct=0.2,
        time_value_am=0.5, iv_ask=0.30, iv_bid=0.29, delta_calc=0.6,
        leverage_calc=8.0,
    )
    row.update(over)
    return row


def _tick(**over):
    kw = dict(code=WARRANT, price=6.5, ts=TS, broker="kgi")
    kw.update(over)
    return Tick(**kw)


def _options():
    common = dict(
        stock_code=CODE, underlying_price=SPOT, days_to_expiry=30,
        ask_live=True, bid_live=True, iv_bid=0.35, volume=100,
    )
    return pd.DataFrame([
        dict(contract="TXO-C600", type="Call", strike=600.0, bid=12.0, ask=12.5, **common),
        dict(contract="TXO-P560", type="Put", strike=560.0, bid=8.0, ask=8.5, **common),
    ])


# ── the merge ────────────────────────────────────────────────────────────────

def test_price_lands_on_ask():
    row = tick_to_warrant_row(_tick(price=6.5), _snapshot())
    assert row["ask"] == 6.5


def test_bid_is_cleared_rather_than_left_stale():
    """A trade print says nothing about the book; keeping the snapshot's bid
    against a fresh ask would invent a spread that never existed."""
    row = tick_to_warrant_row(_tick(price=6.5), _snapshot(bid=4.9))
    assert not row["bid"]


def test_static_metadata_passes_through():
    row = tick_to_warrant_row(_tick(), _snapshot())
    for field in ("warrant_code", "warrant_name", "underlying_code", "type",
                  "underlying_price", "strike", "exercise_ratio",
                  "days_to_expiry", "volume", "ask_qty", "bid_qty"):
        assert row[field] == _snapshot()[field]


def test_snapshot_derived_price_metrics_are_dropped():
    """IV/delta/time value were solved off the snapshot's ask, so they no longer
    describe the tick's price — the matchers re-solve when they are absent."""
    row = tick_to_warrant_row(_tick(), _snapshot())
    for field in ("iv_ask", "iv_bid", "delta_calc", "leverage_calc",
                  "time_value", "time_value_pct", "time_value_am"):
        assert field not in row


def test_days_to_expiry_is_passed_through_unchanged():
    """The snapshot carries no expiry date (CMoney gives only LastDays), so
    there is nothing to recompute the DTE from at tick time."""
    row = tick_to_warrant_row(_tick(), _snapshot(days_to_expiry=42))
    assert row["days_to_expiry"] == 42


def test_tick_provenance_is_attached():
    row = tick_to_warrant_row(_tick(broker="fubon"), _snapshot())
    assert row["tick_ts"] == TS
    assert row["tick_broker"] == "fubon"


def test_inputs_are_not_mutated():
    snap = _snapshot()
    before = dict(snap)
    tick_to_warrant_row(_tick(), snap)
    assert snap == before


def test_accepts_a_dataframe_row():
    df = pd.DataFrame([_snapshot()])
    row = tick_to_warrant_row(_tick(price=7.0), df.iloc[0])
    assert row["ask"] == 7.0
    assert row["warrant_code"] == WARRANT


# ── refusals ─────────────────────────────────────────────────────────────────

def test_mismatched_code_raises():
    """Merging another warrant's print into this row would price an arb off the
    wrong instrument entirely."""
    with pytest.raises(TickTranslationError):
        tick_to_warrant_row(_tick(code="039999"), _snapshot())


def test_missing_or_empty_snapshot_raises():
    with pytest.raises(TickTranslationError):
        tick_to_warrant_row(_tick(), None)
    with pytest.raises(TickTranslationError):
        tick_to_warrant_row(_tick(), {})


def test_non_positive_price_raises():
    """A zero ask makes the warrant leg free and every downstream edge infinite."""
    for bad in (0.0, -1.0):
        with pytest.raises(TickTranslationError):
            tick_to_warrant_row(_tick(price=bad), _snapshot())


def test_non_tick_input_raises():
    with pytest.raises(TickTranslationError):
        tick_to_warrant_row({"code": WARRANT, "price": 6.5}, _snapshot())


# ── the output is what check_tick consumes ───────────────────────────────────

def test_translated_row_drives_check_tick():
    mirror = arb_logic.OptionMirror(_options(), {CODE: CONTRACT_SIZE})
    params = arb_logic.ScanParams(max_strike_diff_pct=10.0, max_dte_diff=40)

    cheap = arb_logic.check_tick(
        tick_to_warrant_row(_tick(price=1.0), _snapshot()), mirror,
        params=params, strategies=["same_type"])
    dear = arb_logic.check_tick(
        tick_to_warrant_row(_tick(price=50.0), _snapshot()), mirror,
        params=params, strategies=["same_type"])

    assert cheap
    assert not dear
