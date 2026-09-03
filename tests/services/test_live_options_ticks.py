"""Book-folding and REST-seed for the Live Options tab (services/live_options.py).

Simulates a websocket tick by calling `_handle_message` directly against a
fake connection and a synthetic JSON frame — no SDK, no live market. This is
new code (copy-adjacent to services/live_warrant.py's `_handle_message`, but
with no dirty-tracking to verify) so it earns its own targeted test rather
than being assumed correct by similarity; the rest of this module is
verified manually against the real Fubon connection, same as live_warrant.py.

The REST-seed tests below stub the SDK's `quote()` call directly rather than
hitting Fugle — `_seed_from_rest`'s one-level-book-from-lastTrade synthesis
is pure enough to unit test even though the rest of the login/subscribe path
isn't.
"""
import json

from services import live_options as lo

CODE = "CDA06500A4"


class _NS:
    """Bare attribute holder — stands in for the nested
    sdk.marketdata.rest_client.futopt.intraday.quote(...) attribute chain."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_sdk(quote_fn):
    return _NS(marketdata=_NS(rest_client=_NS(futopt=_NS(intraday=_NS(quote=quote_fn)))))


def _frame(bids, asks, code=CODE):
    return json.dumps({
        "event": "data", "channel": lo.BOOKS_CHANNEL,
        "data": {"symbol": code, "bids": bids, "asks": asks},
    })


def _clean():
    lo._books.clear()
    lo._seeded.clear()
    lo._track_errors.clear()


def test_handle_message_folds_a_books_tick():
    _clean()
    try:
        lo._handle_message({}, _frame([{"price": 3.2, "size": 5}], [{"price": 3.4, "size": 3}]))
        assert lo._books[CODE]["bids"] == [{"price": 3.2, "size": 5}]
        assert lo._books[CODE]["asks"] == [{"price": 3.4, "size": 3}]
        assert lo._books[CODE]["src"] == "ws"
    finally:
        _clean()


def test_handle_message_second_tick_overwrites_the_book():
    _clean()
    try:
        lo._handle_message({}, _frame([{"price": 3.2, "size": 5}], []))
        lo._handle_message({}, _frame([{"price": 3.3, "size": 6}], [{"price": 3.5, "size": 2}]))
        assert lo._books[CODE]["bids"] == [{"price": 3.3, "size": 6}]
        assert lo._books[CODE]["asks"] == [{"price": 3.5, "size": 2}]
    finally:
        _clean()


def test_handle_message_ignores_frames_without_a_symbol():
    _clean()
    try:
        raw = json.dumps({"event": "data", "channel": lo.BOOKS_CHANNEL, "data": {"bids": [], "asks": []}})
        lo._handle_message({}, raw)
        assert lo._books == {}
    finally:
        _clean()


def test_handle_message_non_books_event_routes_to_control_not_books():
    _clean()
    try:
        raw = json.dumps({
            "event": "subscribed", "data": {"symbol": CODE, "id": "abc123"},
        })
        conn = {"sub_ids": {}}
        lo._handle_message(conn, raw)
        assert conn["sub_ids"].get(CODE) == "abc123"
        assert CODE not in lo._books
    finally:
        _clean()


def test_handle_message_malformed_json_is_a_no_op():
    _clean()
    try:
        lo._handle_message({}, "{not json")
        assert lo._books == {}
    finally:
        _clean()


def test_seed_from_rest_synthesizes_a_one_level_book_from_lasttrade():
    _clean()
    try:
        sdk = _fake_sdk(lambda symbol: {
            "name": "TSMC 202610 CALL 900",
            "lastTrade": {"bid": 3.2, "ask": 3.4, "size": 7},
            "lastUpdated": 1735689600_000000,
        })
        assert lo._seed_from_rest(sdk, CODE) is True
        assert lo._books[CODE]["bids"] == [{"price": 3.2, "size": 7}]
        assert lo._books[CODE]["asks"] == [{"price": 3.4, "size": 7}]
        assert lo._books[CODE]["src"] == "rest"
        assert CODE in lo._seeded
    finally:
        _clean()


def test_seed_from_rest_marks_seeded_even_with_an_empty_book():
    _clean()
    try:
        sdk = _fake_sdk(lambda symbol: {"name": "TSMC", "lastTrade": {}})
        assert lo._seed_from_rest(sdk, CODE) is True
        assert CODE not in lo._books
        assert CODE in lo._seeded
    finally:
        _clean()


def test_seed_from_rest_records_error_after_retries_exhausted(monkeypatch):
    _clean()
    monkeypatch.setattr(lo, "SEED_RETRIES", 1)  # avoid the real backoff sleep in a unit test
    try:
        def _boom(symbol):
            raise RuntimeError("quote failed")
        sdk = _fake_sdk(_boom)
        assert lo._seed_from_rest(sdk, CODE) is False
        assert CODE not in lo._seeded
        assert "seed failed" in lo._track_errors[CODE]
    finally:
        _clean()


def test_seed_from_rest_a_ws_tick_still_overwrites_a_rest_seeded_book():
    _clean()
    try:
        sdk = _fake_sdk(lambda symbol: {"lastTrade": {"bid": 3.2, "ask": 3.4, "size": 7}})
        lo._seed_from_rest(sdk, CODE)
        assert lo._books[CODE]["src"] == "rest"

        lo._handle_message({}, _frame([{"price": 3.25, "size": 4}], [{"price": 3.35, "size": 2}]))
        assert lo._books[CODE]["src"] == "ws"
        assert lo._books[CODE]["bids"] == [{"price": 3.25, "size": 4}]
    finally:
        _clean()
