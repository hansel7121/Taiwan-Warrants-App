"""Book-folding for the Live Options tab (services/live_options.py).

Simulates a websocket tick by calling `_handle_message` directly against a
fake connection and a synthetic JSON frame — no SDK, no live market. This is
new code (copy-adjacent to services/live_warrant.py's `_handle_message`, but
with no dirty-tracking to verify) so it earns its own targeted test rather
than being assumed correct by similarity; the rest of this module is
verified manually against the real Fubon connection, same as live_warrant.py.
"""
import json

from services import live_options as lo

CODE = "CDA06500A4"


def _frame(bids, asks, code=CODE):
    return json.dumps({
        "event": "data", "channel": lo.BOOKS_CHANNEL,
        "data": {"symbol": code, "bids": bids, "asks": asks},
    })


def _clean():
    lo._books.clear()


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
