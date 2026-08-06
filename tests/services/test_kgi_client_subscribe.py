"""KGIClient.subscribe(): the tick callback that turns an SDK tick into a Tick.

Exercises the real `_handle` closure built inside `subscribe()`, against a fake
`Quote` object that only records the callback and echoes it back on demand —
no network, no real kgisuperpy. Stub modules mirror test_broker_tick_time.py's
boundary (kgisuperpy ships in the worker image only).

Issue #54: kgisuperpy's tick object already carries `volume` (this print's
traded quantity) alongside `close`/`datetime`; this is the seam that starts
threading it into the app's own Tick.

Issue #51: same seam, `set_cb_bidask`/`subscribe_bidask` side, for 5-level
depth via the separate `Depth` dataclass.
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


class _FakeBidAsk:
    def __init__(self, symbol, bid_prices, bid_volumes, ask_prices, ask_volumes, dt):
        self.symbol = symbol
        self.bid_prices = bid_prices
        self.bid_volumes = bid_volumes
        self.ask_prices = ask_prices
        self.ask_volumes = ask_volumes
        self.datetime = dt


class _FakeQuote:
    def __init__(self):
        self._cb = None
        self._bidask_cb = None
        self.bidask_subscriptions = []

    def set_cb_tick(self, cb):
        self._cb = cb

    def subscribe_tick(self, code, odd_lot=False):
        pass

    def fire(self, tick):
        self._cb(tick)

    def set_cb_bidask(self, cb):
        self._bidask_cb = cb

    def subscribe_bidask(self, code, odd_lot=False):
        self.bidask_subscriptions.append(code)

    def fire_bidask(self, msg):
        self._bidask_cb(msg)


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


# -- bidask depth (issue #51) -------------------------------------------------

def test_no_depth_subscription_without_an_on_depth_callback():
    """subscribe() with only on_tick must not touch the bidask channel at
    all — Fubon's client shares this signature and has no depth feed yet."""
    client = _client()
    client.subscribe(["031234"], lambda t: None)

    assert client._api.Quote.bidask_subscriptions == []


def test_depth_is_subscribed_alongside_ticks_when_on_depth_is_given():
    client = _client()
    client.subscribe(["031234"], lambda t: None, on_depth=lambda d: None)

    assert client._api.Quote.bidask_subscriptions == ["031234"]


def test_the_depth_carries_all_five_levels_each_side():
    client = _client()
    received = []
    client.subscribe(["031234"], lambda t: None, on_depth=received.append)

    client._api.Quote.fire_bidask(_FakeBidAsk(
        "031234",
        bid_prices=[1.20, 1.19, 1.18, 1.17, 1.16],
        bid_volumes=[10, 20, 30, 40, 50],
        ask_prices=[1.21, 1.22, 1.23, 1.24, 1.25],
        ask_volumes=[5, 15, 25, 35, 45],
        dt="2026-08-06 09:30:00",
    ))

    depth = received[0]
    assert depth.code == "031234"
    assert depth.bid_prices == (1.20, 1.19, 1.18, 1.17, 1.16)
    assert depth.bid_volumes == (10, 20, 30, 40, 50)
    assert depth.ask_prices == (1.21, 1.22, 1.23, 1.24, 1.25)
    assert depth.ask_volumes == (5, 15, 25, 35, 45)
    assert depth.broker == "kgi"
    assert depth.ts.tzinfo is not None
