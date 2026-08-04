"""KGIClient tests.

`kgisuperpy` is worker-only and is not installed in the dev environment, so a
stub module stands in for it. The stub is registered before the client module is
imported, which is also what proves the client touches nothing but the SDK
surface documented in kgi_client.py's module docstring.
"""
import sys
import types
from datetime import datetime

import pytest


_kgisuperpy = types.ModuleType("kgisuperpy")
sys.modules.setdefault("kgisuperpy", _kgisuperpy)

from services.broker import kgi_client  # noqa: E402
from services.broker.base import BrokerConnectionError  # noqa: E402


CREDENTIAL = {
    "person_id": "A123456789",
    "person_pwd": "not-in-any-log",
    "symbols_per_connection": 30,
    "connections": 2,
}


class _FakeQuote:
    def __init__(self):
        self.cb = None
        self.subscribed = []
        self.unsubscribed = []
        self.subscriptions = []

    def set_cb_tick(self, callback):
        self.cb = callback

    def subscribe_tick(self, code, odd_lot=False):
        self.subscribed.append((code, odd_lot))
        self.subscriptions.append(f"qtTick.{code}.v1")

    def get_subscriptions(self):
        return list(self.subscriptions)

    def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)
        self.subscriptions.remove(sub_id)


class _FakeApi:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.Quote = _FakeQuote()
        self.disconnect_cb = None
        self.logged_out = False

    def set_disconnect_cb(self, func):
        self.disconnect_cb = func

    def logout(self):
        self.logged_out = True


class _FakeTick:
    def __init__(self, symbol="031100", close=1.23, dt="2026-08-03 10:15:30.500000"):
        self.symbol = symbol
        self.close = close
        self.datetime = dt


@pytest.fixture
def api(monkeypatch):
    """Swap kgisuperpy.login for a factory that records the login arguments."""
    created = []

    def _login(**kwargs):
        created.append(_FakeApi(**kwargs))
        return created[-1]

    monkeypatch.setattr(kgi_client, "kgisuperpy", types.SimpleNamespace(login=_login))
    return created


def _logged_in_client(api):
    client = kgi_client.KGIClient(CREDENTIAL)
    client.login()
    return client, api[0]


def test_login_passes_the_credential_through_to_the_sdk(api):
    client, fake = _logged_in_client(api)

    assert fake.kwargs == {
        "person_id": "A123456789",
        "person_pwd": "not-in-any-log",
        "simulation": False,
    }
    assert client.is_connected


def test_client_carries_the_accounts_capacity_tier(api):
    client = kgi_client.KGIClient(CREDENTIAL)

    assert (client.symbols_per_connection, client.connections) == (30, 2)


def test_failed_login_raises(monkeypatch):
    monkeypatch.setattr(
        kgi_client,
        "kgisuperpy",
        types.SimpleNamespace(login=lambda **kw: types.SimpleNamespace()),
    )

    with pytest.raises(BrokerConnectionError):
        kgi_client.KGIClient(CREDENTIAL).login()


def test_subscribe_registers_one_callback_and_subscribes_each_code(api):
    client, fake = _logged_in_client(api)

    client.subscribe(["031100", "031101"], lambda tick: None)

    assert fake.Quote.subscribed == [("031100", False), ("031101", False)]
    assert fake.Quote.cb is not None


def test_sdk_tick_becomes_a_tick_on_the_callback(api):
    client, fake = _logged_in_client(api)
    seen = []
    client.subscribe(["031100"], seen.append)

    fake.Quote.cb(_FakeTick())

    (tick,) = seen
    assert (tick.code, tick.price, tick.broker) == ("031100", 1.23, "kgi")
    assert tick.ts == datetime(2026, 8, 3, 10, 15, 30, 500000)


def test_unparseable_tick_timestamp_falls_back_to_arrival_time(api):
    client, fake = _logged_in_client(api)
    seen = []
    client.subscribe(["031100"], seen.append)

    fake.Quote.cb(_FakeTick(dt="10:15:30 上午"))

    assert isinstance(seen[0].ts, datetime)


def test_unsubscribe_maps_codes_back_to_subscription_ids(api):
    client, fake = _logged_in_client(api)
    client.subscribe(["031100", "031101"], lambda tick: None)

    client.unsubscribe(["031101"])

    assert fake.Quote.unsubscribed == ["qtTick.031101.v1"]


def test_disconnect_callback_clears_is_connected(api):
    client, fake = _logged_in_client(api)

    fake.disconnect_cb()

    assert not client.is_connected


def test_logout_closes_the_session(api):
    client, fake = _logged_in_client(api)

    client.logout()

    assert fake.logged_out
    assert not client.is_connected


def test_logout_without_login_is_a_no_op():
    kgi_client.KGIClient(CREDENTIAL).logout()
