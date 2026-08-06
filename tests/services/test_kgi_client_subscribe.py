"""KGIClient.subscribe(): the tick callback that turns an SDK tick into a Tick.

Exercises the real `_handle` closure built inside `subscribe()`, against a fake
`Quote` object that only records the callback and echoes it back on demand —
no network, no real kgisuperpy. Stub modules mirror test_broker_tick_time.py's
boundary (kgisuperpy ships in the worker image only).

Issue #54: kgisuperpy's tick object already carries `volume` (this print's
traded quantity) alongside `close`/`datetime`; this is the seam that starts
threading it into the app's own Tick.
"""
import sys
import types


def _install_sdk_stubs():
    fubon_sdk = types.ModuleType("fubon_neo.sdk")
    fubon_sdk.FubonSDK = object
    fubon = types.ModuleType("fubon_neo")
    fubon.sdk = fubon_sdk
    sys.modules.setdefault("kgisuperpy", types.ModuleType("kgisuperpy"))
    sys.modules.setdefault("fubon_neo", fubon)
    sys.modules.setdefault("fubon_neo.sdk", fubon_sdk)


_install_sdk_stubs()

from services.broker.kgi_client import KGIClient  # noqa: E402


class _FakeTick:
    def __init__(self, symbol, close, volume, dt):
        self.symbol = symbol
        self.close = close
        self.volume = volume
        self.datetime = dt


class _FakeQuote:
    def __init__(self):
        self._cb = None

    def set_cb_tick(self, cb):
        self._cb = cb

    def subscribe_tick(self, code, odd_lot=False):
        pass

    def fire(self, tick):
        self._cb(tick)


class _FakeApi:
    def __init__(self):
        self.Quote = _FakeQuote()


def _client():
    client = KGIClient({"person_id": "x", "person_pwd": "y"})
    client._api = _FakeApi()
    return client


def test_the_tick_carries_the_traded_quantity():
    client = _client()
    received = []
    client.subscribe(["031234"], received.append)

    client._api.Quote.fire(_FakeTick("031234", 1.23, 5000, "2026-08-06 09:30:00"))

    assert received[0].qty == 5000


def test_qty_is_cast_to_int():
    """kgisuperpy declares volume a float property (`_TickData._volume`); the
    app's Tick carries a traded quantity, which is always a whole number of
    units — cast here rather than leaking the SDK's float typing outward."""
    client = _client()
    received = []
    client.subscribe(["031234"], received.append)

    client._api.Quote.fire(_FakeTick("031234", 1.23, 5000.0, "2026-08-06 09:30:00"))

    assert received[0].qty == 5000
    assert isinstance(received[0].qty, int)


def test_the_existing_fields_are_unaffected():
    client = _client()
    received = []
    client.subscribe(["031234"], received.append)

    client._api.Quote.fire(_FakeTick("031234", 1.23, 5000, "2026-08-06 09:30:00"))

    tick = received[0]
    assert tick.code == "031234"
    assert tick.price == 1.23
    assert tick.broker == "kgi"
    assert tick.ts.tzinfo is not None
