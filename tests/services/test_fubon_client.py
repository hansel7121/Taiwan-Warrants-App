"""FubonClient tests.

`fubon_neo` ships as a manual wheel and is not installed here, so a stub module
stands in for it, registered before the client module is imported.
"""
import json
import sys
import types
from datetime import datetime

import pytest


_fubon_neo = types.ModuleType("fubon_neo")
_fubon_sdk = types.ModuleType("fubon_neo.sdk")
_fubon_sdk.FubonSDK = object
_fubon_neo.sdk = _fubon_sdk
sys.modules.setdefault("fubon_neo", _fubon_neo)
sys.modules.setdefault("fubon_neo.sdk", _fubon_sdk)

from services.broker import fubon_client  # noqa: E402
from services.broker.base import BrokerConnectionError  # noqa: E402


CREDENTIAL = {
    "person_id": "A123456789",
    "password": "not-in-any-log",
    "cert_pass": "also-not-in-any-log",
    "cert_path": "/tmp/cert.pfx",
    "symbols_per_connection": 200,
    "connections": 5,
}


def _trade_message(symbol="031100", price=1.23, time=1785000000000000):
    return json.dumps(
        {
            "event": "data",
            "channel": "trades",
            "id": "chan-1",
            "data": {"symbol": symbol, "price": price, "size": 4, "time": time},
        }
    )


class _FakeStock:
    def __init__(self):
        self.handlers = {}
        self.subscribed = []
        self.unsubscribed = []
        self.connected = False
        self.disconnected = False

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, params):
        self.subscribed.append(params)

    def unsubscribe(self, params):
        self.unsubscribed.append(params)


class _FakeSDK:
    instances = []

    def __init__(self):
        self.login_args = None
        self.realtime_ready = False
        self.logged_out = False
        self.marketdata = types.SimpleNamespace(
            websocket_client=types.SimpleNamespace(stock=_FakeStock())
        )
        _FakeSDK.instances.append(self)

    def login(self, *args):
        self.login_args = args
        return types.SimpleNamespace(is_success=True, data=["acct"])

    def init_realtime(self):
        self.realtime_ready = True

    def logout(self):
        self.logged_out = True


@pytest.fixture
def sdk(monkeypatch):
    _FakeSDK.instances = []
    monkeypatch.setattr(fubon_client, "FubonSDK", _FakeSDK)
    return _FakeSDK.instances


def _logged_in_client(sdk):
    client = fubon_client.FubonClient(CREDENTIAL)
    client.login()
    return client, sdk[0]


def test_login_passes_credential_and_initialises_realtime(sdk):
    client, fake = _logged_in_client(sdk)

    assert fake.login_args == (
        "A123456789",
        "not-in-any-log",
        "/tmp/cert.pfx",
        "also-not-in-any-log",
    )
    assert fake.realtime_ready
    assert client.is_connected


def test_client_carries_the_accounts_capacity_tier():
    client = fubon_client.FubonClient(CREDENTIAL)

    assert (client.symbols_per_connection, client.connections) == (200, 5)


def test_failed_login_raises(monkeypatch, sdk):
    monkeypatch.setattr(
        _FakeSDK, "login", lambda self, *a: types.SimpleNamespace(is_success=False)
    )

    with pytest.raises(BrokerConnectionError):
        fubon_client.FubonClient(CREDENTIAL).login()


def test_subscribe_connects_after_registering_handlers(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock

    client.subscribe(["031100", "031101"], lambda tick: None)

    assert "message" in stock.handlers
    assert stock.connected
    assert stock.subscribed == [
        {"channel": "trades", "symbol": "031100"},
        {"channel": "trades", "symbol": "031101"},
    ]


def test_trade_message_becomes_a_tick_on_the_callback(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock
    seen = []
    client.subscribe(["031100"], seen.append)

    stock.handlers["message"](_trade_message())

    (tick,) = seen
    assert (tick.code, tick.price, tick.broker) == ("031100", 1.23, "fubon")
    assert tick.ts == datetime.fromtimestamp(1785000000)


def test_messages_on_other_channels_are_ignored(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock
    seen = []
    client.subscribe(["031100"], seen.append)

    stock.handlers["message"](json.dumps({"event": "data", "channel": "books", "data": {}}))

    assert seen == []


def test_unsubscribe_uses_the_trades_channel(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock
    client.subscribe(["031100"], lambda tick: None)

    client.unsubscribe(["031100"])

    assert stock.unsubscribed == [{"channel": "trades", "symbol": "031100"}]


def test_disconnect_event_clears_is_connected(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock
    client.subscribe(["031100"], lambda tick: None)

    stock.handlers["disconnect"](1006, "closed")

    assert not client.is_connected


def test_logout_closes_the_socket_and_the_session(sdk):
    client, fake = _logged_in_client(sdk)
    stock = fake.marketdata.websocket_client.stock
    client.subscribe(["031100"], lambda tick: None)

    client.logout()

    assert stock.disconnected
    assert fake.logged_out
    assert not client.is_connected


def test_logout_without_login_is_a_no_op():
    fubon_client.FubonClient(CREDENTIAL).logout()
