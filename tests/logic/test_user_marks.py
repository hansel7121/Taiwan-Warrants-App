"""user_marks.quotes: picks the right rows out of the scanner frames."""
import pandas as pd
import pytest

from logic import options_logic
from logic import user_marks
from logic import warrant_logic


WARRANT_ROWS = pd.DataFrame([
    {"warrant_code": "031100", "warrant_name": "TSMC C1", "underlying_code": "2330",
     "type": "Call", "underlying_price": 612.0, "ask": 2.10, "bid": 2.05,
     "days_to_expiry": 90, "strike": 600.0, "exercise_ratio": 0.5, "iv_ask": 0.31},
    {"warrant_code": "099999", "warrant_name": "other", "underlying_code": "2330",
     "type": "Put", "underlying_price": 612.0, "ask": 1.0, "bid": 0.9,
     "days_to_expiry": 30, "strike": 580.0, "exercise_ratio": 1.0, "iv_ask": 0.28},
])

OPTION_ROWS = pd.DataFrame([
    {"contract": "C600 15Aug26", "type": "Call", "underlying_price": 612.0,
     "ask": 18.0, "bid": 17.5, "days_to_expiry": 80, "strike": 600.0,
     "exercise_ratio": 2000, "iv_ask": 0.29, "stock_code": "2330"},
])


@pytest.fixture
def stub_reads(monkeypatch):
    """Capture the kwargs each scanner read is called with."""
    calls = {}

    def fake_warrant(codes, **kwargs):
        calls["warrant"] = (codes, kwargs)
        return WARRANT_ROWS.copy(), None, {}

    def fake_option(codes, **kwargs):
        calls["option"] = (codes, kwargs)
        return OPTION_ROWS.copy(), None, {}

    monkeypatch.setattr(warrant_logic, "read_warrant", fake_warrant)
    monkeypatch.setattr(options_logic, "read_tw_option", fake_option)
    return calls


def test_returns_only_requested_codes(stub_reads):
    out = user_marks.quotes([
        {"kind": "warrant", "code": "031100", "underlying_code": "2330"},
    ])
    assert set(out) == {"warrant:031100"}
    q = out["warrant:031100"]
    assert q["bid"] == 2.05 and q["ask"] == 2.10
    assert q["iv"] == 0.31 and q["underlying_price"] == 612.0
    assert q["exercise_ratio"] == 0.5


def test_filters_are_wide_enough_to_keep_held_instruments(stub_reads):
    user_marks.quotes([{"kind": "warrant", "code": "031100", "underlying_code": "2330"}])
    _codes, kwargs = stub_reads["warrant"]
    # Scanner defaults (max_tv_pct=100, max_days=365, min_volume=0) would drop a
    # deep-OTM or long-dated holding, so quotes() must widen them.
    assert kwargs["max_tv_pct"] >= 1e9
    assert kwargs["max_days"] >= 3650
    assert kwargs["min_volume"] == 0
    assert kwargs["keep_noniv"] is True


def test_tw_option_keyed_by_contract(stub_reads):
    out = user_marks.quotes([
        {"kind": "tw_option", "code": "C600 15Aug26", "underlying_code": "2330"},
    ])
    q = out["tw_option:C600 15Aug26"]
    assert q["bid"] == 17.5 and q["contract_size"] == 2000
    assert q["underlying_code"] == "2330"


def test_underlying_leg_borrows_spot_from_the_warrant_read(stub_reads):
    out = user_marks.quotes([
        {"kind": "underlying", "code": "2330", "underlying_code": "2330"},
    ])
    assert out["underlying:2330"]["underlying_price"] == 612.0
    # No separate spot fetch — it rides the warrant read.
    assert "option" not in stub_reads


def test_missing_instrument_is_absent_not_fatal(stub_reads):
    out = user_marks.quotes([
        {"kind": "warrant", "code": "000000", "underlying_code": "2330"},
    ])
    assert out == {}


def test_empty_input_does_no_fetches(stub_reads):
    assert user_marks.quotes([]) == {}
    assert stub_reads == {}
