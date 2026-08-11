"""Fubon Neo market-data client — one SDK instance, one websocket connection.

Worker-only, like kgi_client.py. `fubon_neo` is not on PyPI (manual wheel from
Fubon's TradeAPI site, see requirements.txt and Dockerfile.worker), so this
module must stay out of every import path the web app touches.

**One connection == one FubonSDK instance with its own realtime websocket
client.** Fubon's rate-limit page states the limits ("5 connections",
"200 subscriptions per connection") without defining what counts as a
connection; underneath, `sdk.init_realtime()` builds exactly one
`fugle_marketdata` WebSocketClient, i.e. one socket, and nothing in the SDK
opens a second one for you. So the safe reading — and the one that matches the
KGI client's shape — is that the pool opens five clients to use five
connections.

Fubon's realtime market data is Fugle's websocket API underneath (fubon_neo's
own adapter imports `fugle_marketdata`), so the wire format here is Fugle's:
subscribe with `{"channel": "trades", "symbol": ...}` — one symbol per
subscribe call, per Fugle's Trades channel docs — and receive
`{"event": "data", "channel": "trades", "data": {...}}` messages.

Credential dict keys: `person_id`, `password`, `cert_pass`, plus `cert_path`
(a local filesystem path — see `from_stored`, which materializes the cert stored
in Supabase Storage, since the SDK's login takes a path rather than bytes).
"""
import json
import os
import tempfile
from datetime import datetime, timezone

from fubon_neo.sdk import FubonSDK

from services.broker import credentials
from services.broker.base import BrokerClient, BrokerConnectionError, Tick


TRADES_CHANNEL = "trades"


class FubonClient(BrokerClient):
    broker = "fubon"

    def __init__(self, credential):
        super().__init__(credential)
        self._sdk = None
        self._ws = None
        self._on_tick = None
        self._streaming = False
        self._connected = False

    @classmethod
    def from_stored(cls, user_id):
        """Credential plus the account's cert written to a local file.

        FubonSDK.login takes a cert *path*, but the cert lives in Supabase
        Storage, so it has to hit the worker's disk somewhere; a temp file keeps
        it out of the repo and out of the image.
        """
        credential = credentials.get_credential(user_id, cls.broker)
        cert = credentials.download_cert(user_id, cls.broker)
        fd, path = tempfile.mkstemp(suffix=".pfx")
        with os.fdopen(fd, "wb") as f:
            f.write(cert)
        return cls({**credential, "cert_path": path})

    def login(self):
        sdk = FubonSDK()
        result = sdk.login(
            self._credential["person_id"],
            self._credential["password"],
            self._credential["cert_path"],
            self._credential["cert_pass"],
        )
        # login returns a result object rather than raising; its message is not
        # included here because it can echo back credential material.
        if not result.is_success:
            raise BrokerConnectionError("Fubon login failed")

        sdk.init_realtime()

        self._sdk = sdk
        self._ws = sdk.marketdata.websocket_client
        self._connected = True

    def logout(self):
        if self._sdk is None:
            return
        self._ws.stock.disconnect()
        self._sdk.logout()
        self._sdk = None
        self._ws = None
        self._streaming = False
        self._connected = False

    def subscribe(self, codes, on_tick, on_depth=None):
        # on_depth accepted but unused: Fugle's depth channel is unconfirmed (#48), so depth stays KGI-only for now (#51).
        self._on_tick = on_tick

        # Handlers must be registered before connect(): the client starts
        # emitting as soon as the socket authenticates. The socket is opened
        # once and kept — a later subscribe() adds codes to the same connection
        # rather than opening a second one, which is what the 5-connection limit
        # counts.
        if not self._streaming:
            self._ws.stock.on("message", self._handle_message)
            self._ws.stock.on("disconnect", self._on_disconnect)
            self._ws.stock.connect()
            self._streaming = True

        for code in codes:
            self._ws.stock.subscribe({"channel": TRADES_CHANNEL, "symbol": code})

    def unsubscribe(self, codes):
        for code in codes:
            self._ws.stock.unsubscribe({"channel": TRADES_CHANNEL, "symbol": code})

    @property
    def is_connected(self):
        return self._connected

    def _handle_message(self, raw):
        message = json.loads(raw)
        if message.get("channel") != TRADES_CHANNEL:
            return

        data = message["data"]
        # qty omitted: Fugle's trades-channel qty field is unconfirmed (#54).
        self._on_tick(
            Tick(
                code=data["symbol"],
                price=float(data["price"]),
                ts=_tick_time(data["time"]),
                broker=self.broker,
            )
        )

    def _on_disconnect(self, *args, **kwargs):
        # Args logged as-is: Fugle's disconnect callback signature isn't
        # documented here, so this is diagnostic-only until we see a real one.
        print(f"FUBON: websocket disconnected, args={args!r} kwargs={kwargs!r}", flush=True)
        self._connected = False


def _tick_time(raw):
    """Fugle timestamps trades in microseconds since the epoch; result is UTC.

    The epoch value is a point in absolute time, so the only choice here is what
    the returned datetime is labelled with: `tz=timezone.utc` makes it explicit
    and container-TZ-independent, where a bare fromtimestamp() would read it as
    system local time and then drop that fact by returning a naive datetime.
    """
    return datetime.fromtimestamp(raw / 1_000_000, tz=timezone.utc)
