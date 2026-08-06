"""KGI QuoteCom/TradeCom client via pythonnet — benchmark-only, issue #47.

Not wired into pool.py's broker dispatch and not reachable from the
self-service credential form: this exists solely so a developer can run
scripts/kgi_client_benchmark.py, which opens this client and kgi_client.py's
KGIClient simultaneously under the same KGI account and compares tick-to-
receipt latency (see docs/research/kgisuperpy-vs-pythonnet-comparison.md).
If the benchmark ever recommends switching production over to this client,
that is a separate follow-up change to pool.py's dispatch table, not this
file.

Unrunnable until two things land by hand (same shape as issue #48's
libCGCrypt.so/fubon_neo blockers, see Dockerfile.worker):
  1. A Mono (or CoreCLR) runtime installed in the worker image — pythonnet's
     `clr` module needs one to load .NET assemblies on Linux.
  2. KGI's QuoteCom .NET DLLs (`Package.dll`, `PushClient.dll`,
     `QuoteCom.dll`, `Interop.KGICGCAPIATLLib.dll`,
     `ICSharpCode.SharpZipLib.dll`) vendored under vendor/kgi_quotecom/ —
     obtained from KGI, not on PyPI, and not committed here since they are
     compiled proprietary binaries (docs/vendor/README.md).

API shape below is ported from the reference proof-of-concept
(github.com/hansel7121/Taiwan-Websocket-Data, QuoteComExamplePy/quotecomPy.py
and to_test/test_subscription_concurrency.py), which only ever ran on
Windows against a live account — every field name and the login handshake
are unverified against this app's Linux/Docker target until #47's benchmark
actually runs.

Credential dict keys: `token`, `sid`, `user_id`, `password` (QuoteCom's own
names — distinct from kgisuperpy's `person_id`/`person_pwd`, since this is a
different KGI product with its own auth). `host`/`port` default to KGI's
published QuoteCom endpoint but may be overridden per credential.
"""
import os
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from services.broker.base import BrokerClient, BrokerConnectionError, Depth, Tick


_TPE = ZoneInfo("Asia/Taipei")

_DEFAULT_HOST = "quoteapi.kgi.com.tw"
_DEFAULT_PORT = 443

# Where the vendored .NET DLLs live once #47's vendoring step lands (see
# module docstring) — not created by this file, only read from.
_VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor", "kgi_quotecom",
)

_LOGIN_TIMEOUT_S = 20


def _load_quotecom():
    """Import pythonnet's `clr` and QuoteCom's .NET types.

    Deferred to call time, not module import time: importing this module
    (e.g. for the benchmark script to reference the class) must not fail
    just because the DLLs aren't vendored yet on a given machine — only
    actually calling login() should.
    """
    import clr

    if _VENDOR_DIR not in sys.path:
        sys.path.append(_VENDOR_DIR)
    clr.AddReference("Package")
    clr.AddReference("PushClient")
    clr.AddReference("QuoteCom")
    from Intelligence import COM_STATUS, DT, QuoteCom
    return COM_STATUS, DT, QuoteCom


class KGIPythonnetClient(BrokerClient):
    broker = "kgi_pythonnet"

    def __init__(self, credential):
        super().__init__(credential)
        self._quote_com = None
        self._connected = False
        self._on_tick = None
        self._on_depth = None
        self._com_status = None
        self._dt = None

    def login(self):
        com_status, dt, QuoteCom = _load_quotecom()
        self._com_status = com_status
        self._dt = dt

        login_ready = threading.Event()
        failed = threading.Event()

        def _on_status(sender, status, msg):
            if status == com_status.LOGIN_READY:
                login_ready.set()
            elif status == com_status.LOGIN_FAIL:
                failed.set()
                login_ready.set()
            elif status == com_status.DISCONNECTED:
                self._connected = False

        quote_com = QuoteCom("", 443, self._credential["sid"], self._credential["token"])
        quote_com.OnGetStatus += _on_status
        quote_com.OnRcvMessage += self._handle_message

        host = self._credential.get("host", _DEFAULT_HOST)
        port = self._credential.get("port", _DEFAULT_PORT)
        quote_com.Connect2Quote(
            host, port, self._credential["user_id"], self._credential["password"], " ", ""
        )

        if not login_ready.wait(timeout=_LOGIN_TIMEOUT_S) or failed.is_set():
            raise BrokerConnectionError("KGI QuoteCom login failed")

        self._quote_com = quote_com
        self._connected = True

    def logout(self):
        if self._quote_com is None:
            return
        self._quote_com.Dispose()
        self._quote_com = None
        self._connected = False

    def subscribe(self, codes, on_tick, on_depth=None):
        self._on_tick = on_tick
        self._on_depth = on_depth
        joined = "|".join(codes)
        self._quote_com.SubQuotesMatch(joined)
        if on_depth is not None:
            self._quote_com.SubQuotesDepth(joined)

    def unsubscribe(self, codes):
        joined = "|".join(codes)
        self._quote_com.UnSubQuotesMatch(joined)
        if self._on_depth is not None:
            self._quote_com.UnSubQuotesDepth(joined)

    @property
    def is_connected(self):
        return self._connected

    def _handle_message(self, sender, pkg):
        dt = self._dt
        if pkg.DT in (dt.QUOTE_STOCK_MATCH1, dt.QUOTE_STOCK_MATCH2,
                      dt.QUOTE_ODD_MATCH1, dt.QUOTE_ODD_MATCH2):
            if self._on_tick is not None:
                self._on_tick(
                    Tick(
                        code=str(pkg.StockNo).strip(),
                        price=float(pkg.Match_Price),
                        ts=_pkg_time(pkg.Match_Time),
                        broker=self.broker,
                        qty=int(pkg.Match_Qty),
                        instrument="warrant",
                    )
                )
        elif pkg.DT in (dt.QUOTE_STOCK_DEPTH1, dt.QUOTE_STOCK_DEPTH2,
                        dt.QUOTE_ODD_DEPTH1, dt.QUOTE_ODD_DEPTH2):
            if self._on_depth is not None:
                self._on_depth(
                    Depth(
                        code=str(pkg.StockNo).strip(),
                        bid_prices=tuple(pkg.BUY_DEPTH[i].PRICE for i in range(5)),
                        bid_volumes=tuple(int(pkg.BUY_DEPTH[i].QUANTITY) for i in range(5)),
                        ask_prices=tuple(pkg.SELL_DEPTH[i].PRICE for i in range(5)),
                        ask_volumes=tuple(int(pkg.SELL_DEPTH[i].QUANTITY) for i in range(5)),
                        ts=_pkg_time(pkg.Match_Time),
                        broker=self.broker,
                        instrument="warrant",
                    )
                )


def _pkg_time(raw):
    """QuoteCom's Match_Time field, falling back to arrival time.

    Same reasoning as kgi_client._tick_time: the reference proof-of-concept
    only ever printed this field, never parsed it, so its exact format is
    unverified until run against a live account. A parse failure must not
    drop the tick.
    """
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now(_TPE)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_TPE)
