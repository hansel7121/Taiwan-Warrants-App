"""KGI SuperPy market-data client — one login, one connection.

Worker-only: `kgisuperpy` is installed in the worker image (Dockerfile.worker,
docs/adr/0010) and nothing the web app imports may reach this module.

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
"""
from datetime import datetime

import kgisuperpy

from services.broker.base import BrokerClient, BrokerConnectionError, Tick


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

    def subscribe(self, codes, on_tick):
        quote = self._api.Quote

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
                )
            )

        quote.set_cb_tick(_handle)
        for code in codes:
            quote.subscribe_tick(code, odd_lot=False)

    def unsubscribe(self, codes):
        # Subscription ids are "{quote_type}.{symbol}.{version}" (e.g.
        # "qtTick.2330.v1"), and unsubscribing takes the id, not the code.
        wanted = set(codes)
        for sub_id in self._api.Quote.get_subscriptions():
            parts = sub_id.split(".")
            if len(parts) > 1 and parts[1] in wanted:
                self._api.Quote.unsubscribe(sub_id)

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
    """
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now()
