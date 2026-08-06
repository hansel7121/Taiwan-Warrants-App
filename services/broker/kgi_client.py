"""KGI SuperPy market-data client — one login, one connection.

Worker-only: `kgisuperpy` is installed in the worker image (Dockerfile.worker)
and nothing the web app imports may reach this module.

**One connection == one `kgisuperpy.login()`.** KGI's own "連線數說明" page is
explicit about it: the account's Capacity Tier grants N connections of M symbols
each, and the documented way to use them all is to create N separate api
instances and spread `subscribe_tick` calls across them ("A+ 級會員可使用 7 條
連線，每條連線最多訂閱 100 檔"). The SDK also exposes an indexed
`api.Quote[i]` lazy list internally, but that is undocumented, so we follow the
documented shape: the Connection Pool constructs one KGIClient per connection,
each logging in on its own. Note the tier numbers in credentials.py (30/2) are a
placeholder — KGI publishes no per-account tier lookup, so the real figures
still need confirming against the live account.

Credential dict keys: `person_id`, `person_pwd` (the SDK's own names), plus the
Capacity Tier columns every stored credential carries.

TAIFEX options (TXO, #55): `api.FutQuote` is a separate quote channel from
`api.Quote` (warrants/stocks), so `subscribe`/`unsubscribe` route each code to
the right one via `_is_option_code` — see that function for the code-format
rule this rests on.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import kgisuperpy

from services.broker.base import BrokerClient, BrokerConnectionError, Depth, Tick


# TWSE trades in exactly one timezone, so every KGI timestamp is Taipei
# wall-clock time — see _tick_time.
_TPE = ZoneInfo("Asia/Taipei")


def _is_option_code(code):
    """True for a letter-first TAIFEX symbol (e.g. TXO...); TWSE stock/warrant codes are all-digits."""
    return bool(code) and not code[0].isdigit()


class KGIClient(BrokerClient):
    broker = "kgi"

    def __init__(self, credential, simulation=False):
        super().__init__(credential)
        self._simulation = simulation
        self._api = None
        self._connected = False

    def login(self):
        api = kgisuperpy.login(
            person_id=self._credential["person_id"],
            person_pwd=self._credential["person_pwd"],
            simulation=self._simulation,
        )
        # The SDK reports a failed login by printing a message and returning a
        # half-built object rather than raising, so a successful login is
        # identified by the market-data attributes it only sets on success.
        if not hasattr(api, "Quote"):
            raise BrokerConnectionError("KGI login failed")

        self._api = api
        self._connected = True
        api.set_disconnect_cb(self._on_disconnect)

    def logout(self):
        if self._api is None:
            return
        self._api.logout()
        self._api = None
        self._connected = False

    def subscribe(self, codes, on_tick, on_depth=None):
        stock_codes = [c for c in codes if not _is_option_code(c)]
        option_codes = [c for c in codes if _is_option_code(c)]
        if stock_codes:
            self._subscribe_quote(self._api.Quote, stock_codes, on_tick, on_depth,
                                  instrument="warrant", odd_lot=False)
        if option_codes:
            self._subscribe_quote(self._api.FutQuote, option_codes, on_tick, on_depth,
                                  instrument="tw_option")

    def _subscribe_quote(self, quote, codes, on_tick, on_depth, instrument, **sub_kwargs):
        """Wire one quote channel (`Quote` or `FutQuote`, #55) the same way: tick + optional bidask."""

        # No annotations on the callback: the SDK rejects a callback whose first
        # parameter is annotated with anything other than its own version-
        # specific tick class, so leaving it bare is what keeps this adapter
        # independent of the SDK's internal types.
        def _handle(tick):
            on_tick(
                Tick(
                    code=tick.symbol,
                    price=float(tick.close),
                    ts=_tick_time(tick.datetime),
                    broker=self.broker,
                    qty=int(tick.volume),
                    instrument=instrument,
                )
            )

        quote.set_cb_tick(_handle)
        for code in codes:
            quote.subscribe_tick(code, **sub_kwargs)

        if on_depth is not None:
            def _handle_bidask(msg):
                on_depth(
                    Depth(
                        code=msg.symbol,
                        bid_prices=tuple(msg.bid_prices),
                        bid_volumes=tuple(msg.bid_volumes),
                        ask_prices=tuple(msg.ask_prices),
                        ask_volumes=tuple(msg.ask_volumes),
                        ts=_tick_time(msg.datetime),
                        broker=self.broker,
                        instrument=instrument,
                    )
                )

            quote.set_cb_bidask(_handle_bidask)
            for code in codes:
                quote.subscribe_bidask(code, **sub_kwargs)

    def unsubscribe(self, codes):
        # Subscription ids are "{quote_type}.{symbol}.{version}" (e.g.
        # "qtTick.2330.v1"), and unsubscribing takes the id, not the code. Both
        # channels are swept since a Watchlist code could be on either (#55).
        wanted = set(codes)
        for quote in (self._api.Quote, self._api.FutQuote):
            for sub_id in quote.get_subscriptions():
                parts = sub_id.split(".")
                if len(parts) > 1 and parts[1] in wanted:
                    quote.unsubscribe(sub_id)

    @property
    def is_connected(self):
        return self._connected

    def _on_disconnect(self):
        self._connected = False


def _tick_time(raw):
    """The tick's exchange timestamp, falling back to arrival time.

    KGI documents `datetime` only as a string and the format is produced inside
    a compiled extension, so it has not been seen against a live account. A
    parse failure must not kill the tick — a slightly late timestamp is a far
    better outcome than a dropped price — hence the one fallback here.

    The result is always tz-aware. A parsed string carries no offset (TWSE has
    only one timezone, so KGI has no reason to print one), and `.replace` labels
    that wall-clock value as Taipei without shifting it — `.astimezone` would be
    wrong here, since it would first read the naive value as *container* local
    time. Storing the timestamp (broker_worker._relay_tick calls .isoformat())
    is only correct if the offset is explicit rather than ambient.
    """
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now(_TPE)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_TPE)
