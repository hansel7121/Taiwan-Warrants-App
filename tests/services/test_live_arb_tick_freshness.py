"""`_format_tick`'s seconds-ago conversion (services/live_arb.py) — the one
piece of the "Last received tick" debug line pure enough to unit test
without a live Fubon session. `_tick_and_freshness`/get_data()/get_lp_data()
read the real Live Warrant/Live Options module state, so — like the rest of
this tab's service layer — those stay manually verified rather than unit
tested.
"""
from datetime import datetime, timedelta, timezone

from services import live_arb as la


def test_format_tick_none_stays_none():
    assert la._format_tick(None) is None


def test_format_tick_computes_seconds_ago_and_keeps_the_price_fields():
    ts = datetime.now(timezone.utc) - timedelta(seconds=3)
    tick = {"kind": "option", "code": "CDA06500A4", "name": "TSMC Call",
            "bid": 3.2, "ask": 3.4, "ts": ts}
    out = la._format_tick(tick)
    assert out["kind"] == "option"
    assert out["code"] == "CDA06500A4"
    assert out["name"] == "TSMC Call"
    assert out["bid"] == 3.2
    assert out["ask"] == 3.4
    assert 2.5 <= out["seconds_ago"] <= 4.0
    assert "ts" not in out  # raw datetime never leaks into the JSON payload
