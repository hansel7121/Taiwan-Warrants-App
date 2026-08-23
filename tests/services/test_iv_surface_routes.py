"""The IV-surface routes: one underlying, both quote sides, and the bound filters.

The surface is built for a single underlying — two of them share no strike axis,
so interpolating across the pair draws the triangulation rather than the market.
Every plot carries a bid sheet and an ask sheet built from their own IV columns
and filtered independently, so an instrument quoting only one side appears on
only that sheet.
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("LOCAL_USER_ID", "user-a")
os.environ["APP_ENV"] = "local"

import app as app_module  # noqa: E402
from logic import iv_surface, options_logic, warrant_logic  # noqa: E402


def warrants(n=24, iv_ask=0.45, iv_bid=0.40, dte=60, strike0=560.0):
    """A grid of strikes x expiries — a surface needs spread in both axes."""
    rows = []
    for i in range(n):
        rows.append({
            "warrant_code": f"03{i:04d}", "warrant_name": f"W{i}",
            "underlying_code": "2330", "type": "Call", "underlying_price": 600.0,
            "ask": 2.0, "bid": 1.9, "ask_qty": 10, "bid_qty": 10,
            "days_to_expiry": dte + (i % 4) * 20,
            "strike": strike0 + (i // 4) * 10.0, "exercise_ratio": 0.5, "volume": 100,
            "time_value": 1.0, "bid_time_value_pct": 1.0, "ask_time_value_pct": 1.0,
            "time_value_am": 1.0,
            "iv_ask": iv_ask + (i % 5) * 0.01, "iv_bid": iv_bid + (i % 5) * 0.01,
            "delta_calc": 0.5, "leverage_calc": 3.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(warrant_logic, "read_warrant",
                        lambda *a, **k: (warrants(), None, {"source": "test"}))
    return app_module.app.test_client()


def post(client, body):
    return client.post("/iv_surface", json=body).get_json()


def test_two_underlyings_are_refused(client):
    out = post(client, {"stock_codes": ["2330", "2303"]})
    assert out["error"] == "Please select only one underlying stock."
    assert "ask" not in out


def test_no_underlying_is_refused(client):
    assert post(client, {"stock_codes": []})["error"] == "Please select an underlying stock."


def test_one_underlying_returns_both_sides(client):
    out = post(client, {"stock_codes": ["2330"]})
    assert out.get("error") is None
    for side in ("ask", "bid"):
        assert out[side] is not None, side
        assert len(out[side]["x"]) == iv_surface.DEFAULT_RESOLUTION
        assert len(out[side]["z"]) == iv_surface.DEFAULT_RESOLUTION
        assert out[side]["scatter_x"] and out[side]["labels"]


def test_the_two_sheets_carry_different_vol(client):
    """The gap between them is the bid/ask vol spread, which one mid sheet hides."""
    out = post(client, {"stock_codes": ["2330"]})
    assert min(out["ask"]["scatter_z"]) > max(out["bid"]["scatter_z"]) - 10
    assert out["ask"]["scatter_z"] != out["bid"]["scatter_z"]


def test_iv_bounds_are_in_vol_points_and_per_side(monkeypatch):
    """A band that admits the ask IVs but not the bid ones leaves only one sheet."""
    monkeypatch.setattr(warrant_logic, "read_warrant",
                        lambda *a, **k: (warrants(iv_ask=0.60, iv_bid=0.30), None, {}))
    c = app_module.app.test_client()
    out = post(c, {"stock_codes": ["2330"], "iv_min": 55, "iv_max": 70})
    assert out["ask"] is not None
    assert out["bid"] is None


def test_a_missing_or_blank_bound_is_unbounded(monkeypatch):
    """The route applies no IV band of its own. The 20-100 default lives in the
    UI, where it is visible and can be cleared — a band applied silently
    server-side would drop points with nothing on screen to explain it."""
    monkeypatch.setattr(warrant_logic, "read_warrant",
                        lambda *a, **k: (warrants(iv_ask=2.5, iv_bid=2.4), None, {}))
    c = app_module.app.test_client()
    for body in ({"stock_codes": ["2330"]},
                 {"stock_codes": ["2330"], "iv_min": "", "iv_max": None}):
        out = post(c, body)
        assert out.get("error") is None
        assert min(out["ask"]["scatter_z"]) > 100      # 250 vol pts, kept

    banded = post(c, {"stock_codes": ["2330"], "iv_min": 20, "iv_max": 100})
    assert banded.get("error")                          # same points, now excluded


def test_dte_and_strike_bounds_shrink_the_point_set(client):
    full = post(client, {"stock_codes": ["2330"]})
    dte = post(client, {"stock_codes": ["2330"], "dte_max": 80})
    strike = post(client, {"stock_codes": ["2330"], "strike_min": 590})
    assert len(dte["ask"]["scatter_x"]) < len(full["ask"]["scatter_x"])
    assert max(dte["ask"]["scatter_y"]) <= 80
    assert min(strike["ask"]["scatter_x"]) >= 590


def test_filters_that_pass_nothing_are_an_error_not_an_empty_plot(client):
    out = post(client, {"stock_codes": ["2330"], "dte_min": 9000})
    assert "error" in out and out["error"]


def test_highlight_sits_on_the_ask_sheet(client):
    out = post(client, {"stock_codes": ["2330"], "highlight_code": "030007"})
    h = out["highlight"]
    assert h["code"] == "030007"
    assert h["z"] == pytest.approx(min(out["ask"]["scatter_z"]), abs=50)


def test_options_route_also_takes_one_product(monkeypatch):
    monkeypatch.setattr(options_logic, "read_tw_option",
                        lambda *a, **k: (pd.DataFrame(), None, {}))
    c = app_module.app.test_client()
    out = c.post("/iv_surface_options", json={"stock_codes": ["2330", "2303"]}).get_json()
    assert out["error"] == "Please select only one underlying stock."


def test_surface_needs_three_points():
    """Below three points there is nothing to triangulate."""
    assert iv_surface.surface_from_points([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], ["a", "b"]) is None
    out = iv_surface.surface_from_points([1.0, 2.0, 3.0], [1.0, 3.0, 2.0],
                                         [10.0, 20.0, 30.0], ["a", "b", "c"], resolution=8)
    assert out is not None and len(out["x"]) == 8
