"""Pure-logic tests for logic/live_arb_logic.py.

Only `latest_tick` is covered here — the matching path (`scan`/
`build_direct_arrays`) reuses the same `direct_pairs` kernel arb_logic.py's
Direct Match already exercises via tests/logic/test_arb_golden.py, so it
isn't re-covered by a second fixture set here.
"""
from datetime import datetime, timedelta, timezone

from logic import live_arb_logic as lal

NOW = datetime(2026, 9, 2, 5, 0, 0, tzinfo=timezone.utc)


def _row(code, ts, src="ws", bid=3.2, ask=3.4, name=None):
    return {
        "code": code, "name": name or code,
        "best": {"bid": bid, "ask": ask},
        "ts": ts, "src": src,
    }


def test_latest_tick_none_when_nothing_has_ticked():
    assert lal.latest_tick([], []) is None


def test_latest_tick_ignores_rows_with_no_ts_at_all():
    row = {"code": "A", "name": "A", "best": {}, "ts": None, "src": "ws"}
    assert lal.latest_tick([row], []) is None


def test_latest_tick_ignores_rest_seeded_rows():
    """A REST-seeded book fills a cell before any tick arrives, but it isn't
    a tick the exchange pushed and never bumps the tick-seq counters — see
    the function's own docstring for why "ws" is the only source that
    counts here."""
    rest_only = _row("W1", NOW, src="rest")
    assert lal.latest_tick([rest_only], []) is None


def test_latest_tick_picks_the_more_recent_of_warrant_and_option():
    older = _row("W1", NOW - timedelta(seconds=5))
    newer = _row("CDA06500A4", NOW)
    tick = lal.latest_tick([older], [newer])
    assert tick["code"] == "CDA06500A4"
    assert tick["kind"] == "option"


def test_latest_tick_picks_the_more_recent_warrant_over_an_older_option():
    newer = _row("W1", NOW)
    older = _row("CDA06500A4", NOW - timedelta(seconds=5))
    tick = lal.latest_tick([newer], [older])
    assert tick["code"] == "W1"
    assert tick["kind"] == "warrant"


def test_latest_tick_reports_bid_ask_and_name_from_the_winning_row():
    row = _row("W1", NOW, bid=1.11, ask=1.22, name="TSMC Call")
    tick = lal.latest_tick([row], [])
    assert tick == {"kind": "warrant", "code": "W1", "name": "TSMC Call",
                     "bid": 1.11, "ask": 1.22, "ts": NOW}


def test_latest_tick_ignores_rest_rows_even_when_they_are_the_newest():
    ws_row = _row("W1", NOW - timedelta(seconds=5), src="ws")
    rest_row = _row("W2", NOW, src="rest")
    tick = lal.latest_tick([ws_row, rest_row], [])
    assert tick["code"] == "W1"
