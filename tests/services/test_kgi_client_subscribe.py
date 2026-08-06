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

Issue #55: a second fake channel, `_FakeFutQuote`, stands in for
`api.FutQuote` — the TAIFEX options entry point, routed to by code shape
(`_is_option_code`) rather than by any per-call instrument argument.
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
    """Stands in for `api.Quote` (stock/warrant channel)."""

    def __init__(self):
        self._cb = None
        self._bidask_cb = None
        self.tick_subscriptions = []
        self.bidask_subscriptions = []
        self._subs = []

    def set_cb_tick(self, cb):
        self._cb = cb

    def subscribe_tick(self, code, odd_lot=False):
        self.tick_subscriptions.append(code)
        self._subs.append(f"qtTick.{code}.v1")

    def fire(self, tick):
        self._cb(tick)

    def set_cb_bidask(self, cb):
        self._bidask_cb = cb

    def subscribe_bidask(self, code, odd_lot=False):
        self.bidask_subscriptions.append(code)
        self._subs.append(f"qtBidAsk.{code}.v1")

    def fire_bidask(self, msg):
        self._bidask_cb(msg)

    def get_subscriptions(self):
        return list(self._subs)

    def unsubscribe(self, sub_id):
        self._subs.remove(sub_id)


class _FakeFutQuote:
    """Stands in for `api.FutQuote` (#55); unlike `_FakeQuote` takes no `odd_lot`, so a stray call raises TypeError here."""

    def __init__(self):
        self._cb = None
        self._bidask_cb = None
        self.tick_subscriptions = []
        self.bidask_subscriptions = []
        self._subs = []

    def set_cb_tick(self, cb):
        self._cb = cb

    def subscribe_tick(self, code):
        self.tick_subscriptions.append(code)
        self._subs.append(f"qtTick.{code}.v0")

    def fire(self, tick):
        self._cb(tick)

    def set_cb_bidask(self, cb):
        self._bidask_cb = cb

    def subscribe_bidask(self, code):
        self.bidask_subscriptions.append(code)
        self._subs.append(f"qtBidAsk.{code}.v0")

    def fire_bidask(self, msg):
        self._bidask_cb(msg)

    def get_subscriptions(self):
        return list(self._subs)

    def unsubscribe(self, sub_id):
        self._subs.remove(sub_id)


class _FakeApi:
    def __init__(self):
        self.Quote = _FakeQuote()
        self.FutQuote = _FakeFutQuote()


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


def test_a_warrant_tick_is_tagged_warrant():
    client = _client()
    received = []
    client.subscribe(["031234"], received.append)

    client._api.Quote.fire(_FakeTick("031234", 1.23, 5000, "2026-08-06 09:30:00"))

    assert received[0].instrument == "warrant"


# -- TAIFEX options via FutQuote (issue #55) ----------------------------------

def test_an_option_code_is_subscribed_on_futquote_not_quote():
    client = _client()
    client.subscribe(["TXO20500Q6"], lambda t: None)

    assert client._api.FutQuote.tick_subscriptions == ["TXO20500Q6"]
    assert client._api.Quote.tick_subscriptions == []


def test_a_warrant_code_never_touches_futquote():
    client = _client()
    client.subscribe(["031234"], lambda t: None)

    assert client._api.Quote.tick_subscriptions == ["031234"]
    assert client._api.FutQuote.tick_subscriptions == []


def test_mixed_codes_are_split_across_both_channels():
    client = _client()
    client.subscribe(["031234", "TXO20500Q6"], lambda t: None)

    assert client._api.Quote.tick_subscriptions == ["031234"]
    assert client._api.FutQuote.tick_subscriptions == ["TXO20500Q6"]


def test_an_option_tick_is_tagged_tw_option():
    client = _client()
    received = []
    client.subscribe(["TXO20500Q6"], received.append)

    client._api.FutQuote.fire(_FakeTick("TXO20500Q6", 45.0, 3, "2026-08-06 09:30:00"))

    tick = received[0]
    assert tick.code == "TXO20500Q6"
    assert tick.price == 45.0
    assert tick.qty == 3
    assert tick.instrument == "tw_option"
    assert tick.broker == "kgi"


def test_option_depth_is_subscribed_and_tagged():
    client = _client()
    received = []
    client.subscribe(["TXO20500Q6"], lambda t: None, on_depth=received.append)

    assert client._api.FutQuote.bidask_subscriptions == ["TXO20500Q6"]

    client._api.FutQuote.fire_bidask(_FakeBidAsk(
        "TXO20500Q6",
        bid_prices=[45.0, 44.5, 44.0, 43.5, 43.0],
        bid_volumes=[1, 2, 3, 4, 5],
        ask_prices=[45.5, 46.0, 46.5, 47.0, 47.5],
        ask_volumes=[1, 2, 3, 4, 5],
        dt="2026-08-06 09:30:00",
    ))

    depth = received[0]
    assert depth.instrument == "tw_option"
    assert depth.bid_prices == (45.0, 44.5, 44.0, 43.5, 43.0)


def test_unsubscribe_sweeps_both_channels():
    client = _client()
    client.subscribe(["031234", "TXO20500Q6"], lambda t: None)

    client.unsubscribe(["031234", "TXO20500Q6"])

    assert client._api.Quote.get_subscriptions() == []
    assert client._api.FutQuote.get_subscriptions() == []


def test_unsubscribe_leaves_untouched_codes_alone():
    client = _client()
    client.subscribe(["031234", "031999", "TXO20500Q6"], lambda t: None)

    client.unsubscribe(["031234"])

    assert client._api.Quote.tick_subscriptions == ["031234", "031999"]
    assert [s for s in client._api.Quote.get_subscriptions() if "031234" in s] == []
    assert any("031999" in s for s in client._api.Quote.get_subscriptions())
    assert any("TXO20500Q6" in s for s in client._api.FutQuote.get_subscriptions())
