"""`_format_tick`'s seconds-ago conversion and `_tick_and_freshness`'s
heartbeat check (services/live_arb.py) — the pieces of the "Last received
tick" debug line pure enough to unit test without a live Fubon session (with
both Live Warrant/Live Options tracking nothing, `_combined_seq()` inside
`_tick_and_freshness` reads back empty in-memory state rather than needing a
real session). get_data()/get_lp_data() themselves stay manually verified,
like the rest of this tab's service layer.

`_tick_and_freshness` used to compare a freshly re-fetched combined tick-seq
against the seq the scan loop last saw — see its current docstring for why
that was wrong: it reported NOT up to date almost constantly because ticks
arrive continuously, so some newer tick had almost always landed by the time
the fresh fetch ran, regardless of how fast the loop itself was. These tests
pin the heartbeat replacement (loop-alive-within-STALE_AFTER_S), not the seq
comparison.
"""
import time
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


def test_tick_and_freshness_up_to_date_when_enabled_and_recently_scanned():
    _, up_to_date = la._tick_and_freshness(True, time.monotonic())
    assert up_to_date is True


def test_tick_and_freshness_not_up_to_date_when_disabled():
    # A live heartbeat (just recorded) but the loop is stopped -- e.g. right
    # after Stop, before the recorded timestamp itself goes stale. `enabled`
    # must gate this immediately rather than waiting out STALE_AFTER_S.
    _, up_to_date = la._tick_and_freshness(False, time.monotonic())
    assert up_to_date is False


def test_tick_and_freshness_not_up_to_date_when_never_scanned():
    _, up_to_date = la._tick_and_freshness(True, None)
    assert up_to_date is False


def test_tick_and_freshness_not_up_to_date_once_stale():
    stale = time.monotonic() - (la.STALE_AFTER_S + 0.5)
    _, up_to_date = la._tick_and_freshness(True, stale)
    assert up_to_date is False


def test_tick_and_freshness_does_not_flip_stale_just_shy_of_the_threshold():
    fresh = time.monotonic() - (la.STALE_AFTER_S - 0.1)
    _, up_to_date = la._tick_and_freshness(True, fresh)
    assert up_to_date is True
