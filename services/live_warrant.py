"""Live Warrant tab session: one Fubon account, a lazily-grown pool of
websocket connections, and the in-process order-book cache the /data route
reads from.

Adapted from the standalone scripts/fubon_quote_viewer.py (single connection,
no persistence) into a reusable multi-connection session: app.py routes and
services/scheduler.py's trading-hour gate call start_session()/reconnect()/
add_code()/remove_code()/scan_underlying()/get_data(); the tracked list they
operate on is persisted via services/db_live_warrant.py. Connection
assignment, the subscription cap, and scan-vs-manual replace decisions are
delegated to logic/live_warrant_logic.py (pure, unit tested); this module is
the impure glue around them and is verified manually against the real Fubon
connection during trading hours, not by the test suite.
"""
import collections
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from logic import iv_engine, live_warrant_logic
from services import db_live_warrant
from services.broker import credentials as broker_credentials

FUBON_CRED_LABEL = os.environ.get("FUBON_CRED_LABEL", broker_credentials.DEFAULT_LABEL)
BOOKS_CHANNEL = "books"

RECONNECT_RETRIES = 3
RECONNECT_BACKOFF_S = 5

# Same TWSE MIS volume source scripts/fubon_quote_viewer.py uses for its
# liquidity ranking (Fugle has no bulk warrant quote endpoint).
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_BATCH = 100
MIS_WORKERS = 6
# Parallel REST book seeds during a bulk scan. Bounded well under the per-second
# quota the Fugle REST endpoint enforces; raising it trades scan latency for a
# higher chance of being throttled mid-scan.
SEED_WORKERS = 8
# The REST seed is the one call that gets throttled in bulk, and a throttled
# seed used to be permanent: the code kept whatever placeholder name the failure
# wrote and was never asked again. Retry it, then hand the leftovers to
# retry_pending().
SEED_RETRIES = 3
SEED_BACKOFF_S = 0.6
# Measured against this account on 2026-08-25: the Fugle REST quota is ~300
# requests per rolling minute, shared across EVERY endpoint — once quotes
# exhaust it, `intraday/tickers` 429s too. 800 quotes at 8 unbounded workers got
# 299 through and then failed every remaining call with
# "FugleAPIError 429 Rate limit exceeded"; the same 300 requests paced at 5/s
# ran clean with zero errors. This is the ceiling every REST call now waits for
# rather than discovers.
REST_QUOTA = 300
REST_QUOTA_WINDOW_S = 60.0
# Longest a single call will block waiting for a slot before giving up. A scan
# leaves what it can't seed for retry_pending(), so waiting out a whole window
# inside a web request is never worth it.
REST_ACQUIRE_TIMEOUT_S = 20.0
# REST seeds a single scan request will spend inline. At 300/min a full TSMC
# chain (~1,050 books) is 3.5 minutes of seeding — well past gunicorn's request
# timeout — so the scan seeds what fits and retry_pending() drains the rest.
SEED_BUDGET_PER_SCAN = 200
# Contract terms come from intraday/ticker, which is a second call per code on
# top of the book seed. They are static for a warrant's whole life, so they are
# fetched once, cached, and never refreshed — and never fetched inline during a
# scan, where they would double an already quota-bound request.
TERMS_RETRY_BATCH = 60
TW_TZ = ZoneInfo("Asia/Taipei")
MIS_RETRIES = 3
MIS_BACKOFF_S = 1.0
# Codes re-attempted per retry_pending() call. Bounded so a scheduler tick that
# inherits a fully failed 1,000-code scan still finishes promptly.
PENDING_RETRY_BATCH = 100
# Warrants are listed on both boards; scanning only TSE silently misses the
# OTC-listed half of some chains.
WARRANT_MARKETS = ("TSE", "OTC")
MIS_HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://mis.twse.com.tw/stock/index.jsp"}

_lock = threading.RLock()
_books = {}       # code -> {"bids": [...], "asks": [...], "ts": datetime, "src": "ws"|"rest"}
_names = {}       # code -> name
_tracked = []     # codes, add order — mirrors live_warrant_tracked
_connections = [] # each: {"sdk", "ws", "codes": set(), "sub_ids": {}, "state", "last_error"}
_seeded = set()   # codes whose REST seed completed (an empty book still counts)
_terms = {}       # code -> {"strike", "exercise_ratio", "maturity"}; static, fetched once
_seed_errors = {}  # code -> last REST seed/subscribe error, cleared once it succeeds
_pending = set()  # tracked codes with no live subscription — retried by retry_pending()
_session_error = None  # set when the session can't even start (e.g. no credentials)
_stopped_by_user = False  # set by stop_session(); blocks scheduler/add_code from silently reopening it

# ── Live Warrant tab analytics: tick-driven dirty tracking ─────────────────
# A warrant's derived columns are only ever recomputed when ITS OWN best bid/
# ask moves (see _handle_message/_recompute_if_dirty) — never on a timer, and
# never because the underlying or a sibling warrant ticked. This is what keeps
# an illiquid warrant from burning compute it never asked for.
_book_seq = {}        # code -> int, bumped only when the code's best level changes
_computed = {}         # code -> {"seq", "time_value", "bid_time_value_pct", "ask_time_value_pct"}
_pending_log = {}      # code -> deque of {"seq","ts","diff"} awaiting a recompute to log against
_underlying_of = {}    # code -> underlying stock code, mirrors live_warrant_tracked.underlying
_underlying_books = {}  # underlying code -> {"price", "best", "ts", "src"}
_underlying_codes = set()  # underlying codes currently subscribed on the same connection pool
_underlying_names = {}  # underlying code -> display name, from the same ticker lookup a scan already makes
_volumes = {}           # code -> int, MIS-sourced, refreshed on the retry_pending() cadence
_console_log = collections.deque(maxlen=1000)
_console_log_next_id = 0
CONSOLE_LOG_PER_CODE = 20  # queued-but-not-yet-recomputed diffs kept per code before the oldest drops
VOLUME_RETRY_BATCH = 300   # tracked codes refreshed per retry_pending() call — cheap, bypasses the Fugle quota
# Underlyings whose own price is subscribed live (books-channel midpoint) for
# tick-driven recompute. Scoped to TSMC only for initial validation against a
# real, highly liquid symbol before any wider rollout — see
# docs/fubon_connection_runbook.md and the plan this shipped under. Trivially
# widened once confirmed correct.
UNDERLYING_LIVE_PRICE_ALLOWLIST = {"2330"}


# ─────────────────────────────────────────────────────────────────────────────
# REST quota
# ─────────────────────────────────────────────────────────────────────────────
class _RestQuota:
    """Sliding-window limiter in front of every Fugle REST call.

    The SDK does nothing to pace requests, so the seed fan-out used to find the
    quota by hitting it: the first ~300 calls succeeded and every one after that
    came back 429 until the window rolled. Waiting for a slot turns that into
    latency instead of into a wall of failed seeds — which is what was showing
    up in the name column.

    A sliding window rather than a token bucket because that is what the server
    enforces: the budget refills as individual requests age out, not in one lump.
    """

    def __init__(self, limit=REST_QUOTA, window=REST_QUOTA_WINDOW_S):
        self._limit = limit
        self._window = window
        self._hits = collections.deque()
        self._lock = threading.Lock()

    def acquire(self, timeout=None):
        """Claim one request slot. False if `timeout` elapsed without one."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= self._window:
                    self._hits.popleft()
                if len(self._hits) < self._limit:
                    self._hits.append(now)
                    return True
                wait = self._window - (now - self._hits[0])
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                wait = min(wait, left)
            # Short slices so freed slots are picked up promptly and the waiting
            # threads don't all wake on the same instant.
            time.sleep(min(wait, 0.25) + 0.01)

    def used(self):
        with self._lock:
            now = time.monotonic()
            while self._hits and now - self._hits[0] >= self._window:
                self._hits.popleft()
            return len(self._hits)


_rest_quota = _RestQuota()


def _rest_call(label, fn, **params):
    """Run one REST call against the shared quota."""
    if not _rest_quota.acquire(timeout=REST_ACQUIRE_TIMEOUT_S):
        raise RuntimeError(
            f"REST quota exhausted: no slot for {label} within {REST_ACQUIRE_TIMEOUT_S:.0f}s "
            f"({REST_QUOTA}/{REST_QUOTA_WINDOW_S:.0f}s)")
    return fn(**params)


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics helpers
# ─────────────────────────────────────────────────────────────────────────────
def _error_detail(e):
    """One readable line for an SDK error.

    FugleAPIError's str() is a multi-line block carrying the URL, the params and
    a 200-character dump of the response body; the status code and message are
    the only parts worth putting in a log line or the status bar. Keeping them
    is the difference between "(FugleAPIError)" — which says nothing — and
    "FugleAPIError 429 Too Many Requests", which names the fix.
    """
    status = getattr(e, "status_code", None)
    message = getattr(e, "message", None)
    if status or message:
        return " ".join(str(x) for x in (type(e).__name__, status, message) if x)
    return f"{type(e).__name__}: {e}"


def _is_placeholder_name(name):
    """Whether a persisted name is one of the old exception placeholders.

    Rows written before the seed path stopped renaming instruments still carry
    "(FugleAPIError)" (or "(ConnectionError)", …) in live_warrant_tracked.name.
    Treating those as "no name" is what lets retry_pending() re-fetch the real
    one and update_name() overwrite the row — the pollution repairs itself on
    the next trading-hour tick instead of needing a manual backfill.
    """
    name = (name or "").strip()
    return name.startswith("(") and name.endswith(")")


def _display_name(code):
    """Cached name, falling back to the code itself.

    Never returns a placeholder built from an exception class: that string used
    to be written into `_names` and from there persisted into
    live_warrant_tracked.name, so one throttled REST call renamed the warrant
    permanently.
    """
    return _names.get(code) or code


# ─────────────────────────────────────────────────────────────────────────────
# Login / connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def _login_new_connection():
    """Log in fresh and open one websocket connection. Raises on any failure.

    Callbacks close over the `conn` dict directly, not a list index: the
    "authenticated" control message arrives during ws.connect()'s blocking
    handshake, before this connection exists in `_connections`, so indexing
    into the list from the callback would IndexError inside the SDK's own
    callback thread — which silently starves connect()'s wait loop into an
    "authentication timeout" instead of surfacing the real error.
    """
    creds = broker_credentials.get_credential(FUBON_CRED_LABEL)
    if creds is None:
        raise RuntimeError(f"no fubon_credentials row for label '{FUBON_CRED_LABEL}'")
    missing = [k for k in ("fubon_id", "fubon_password", "cert_password") if not creds.get(k)]
    if missing:
        raise RuntimeError(f"fubon_credentials row '{FUBON_CRED_LABEL}' missing: {', '.join(missing)}")
    if not creds.get("cert_path"):
        raise RuntimeError(f"fubon_credentials row '{FUBON_CRED_LABEL}' has no cert uploaded")

    cert_bytes = broker_credentials.download_cert(FUBON_CRED_LABEL)

    from fubon_neo.sdk import FubonSDK

    cert_ext = creds["cert_path"].rsplit(".", 1)[-1] if "." in creds["cert_path"] else "p12"
    fd, local_cert_path = tempfile.mkstemp(suffix=f".{cert_ext}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cert_bytes)

        sdk = FubonSDK()
        result = sdk.login(creds["fubon_id"], creds["fubon_password"],
                           local_cert_path, creds["cert_password"])
        if not getattr(result, "is_success", False):
            raise RuntimeError(f"login failed: {getattr(result, 'message', result)}")

        sdk.init_realtime()
        ws = sdk.marketdata.websocket_client.stock
        conn = {"sdk": sdk, "ws": ws, "codes": set(), "sub_ids": {}, "state": "connecting", "last_error": None}
        # Before connect(): the socket starts emitting as soon as it authenticates.
        ws.on("message", lambda raw: _handle_message(conn, raw))
        ws.on("connect", lambda *a, **k: _on_connect(conn))
        ws.on("disconnect", lambda *a, **k: _on_disconnect(conn))
        ws.on("error", lambda *a, **k: _on_error(conn, a, k))
        ws.connect()
    finally:
        try:
            os.unlink(local_cert_path)
        except OSError:
            pass

    return conn


def _ensure_connection(idx):
    """Open pool connection `idx` if it doesn't exist yet — connections only ever grow by one."""
    with _lock:
        if idx < len(_connections):
            return _connections[idx]
        if _stopped_by_user:
            raise RuntimeError("session disconnected — click Connect to resume")
    conn = _login_new_connection()
    with _lock:
        conn["index"] = len(_connections)
        _connections.append(conn)
    return conn


def _handle_message(conn, raw):
    """Fold one books message into the shared cache; the SDK hands us the raw frame."""
    import json
    try:
        message = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return

    event = message.get("event")
    if event != "data" or message.get("channel") != BOOKS_CHANNEL:
        _handle_control(conn, event, message)
        return

    data = message.get("data") or {}
    code = data.get("symbol")
    if not code:
        return

    new_bids, new_asks = data.get("bids") or [], data.get("asks") or []
    with _lock:
        if code in _underlying_codes:
            _fold_underlying_tick_locked(code, new_bids, new_asks)
            return
        old_book = _books.get(code)
        dirty = live_warrant_logic.best_level_changed(old_book, new_bids, new_asks)
        _books[code] = {
            "bids": new_bids,
            "asks": new_asks,
            "ts": datetime.now(timezone.utc),
            "src": "ws",
        }
        # A level-2..5-only requote does not dirty the code: every displayed
        # column depends solely on the best level, so recomputing here would
        # burn exactly the compute this design exists to avoid.
        if dirty:
            _book_seq[code] = _book_seq.get(code, 0) + 1
            diff = live_warrant_logic.describe_book_change(old_book, new_bids, new_asks)
            _pending_log.setdefault(code, collections.deque(maxlen=CONSOLE_LOG_PER_CODE)).append(
                {"seq": _book_seq[code], "ts": datetime.now(timezone.utc), "diff": diff})


def _fold_underlying_tick_locked(code, bids, asks):
    """Best-bid/best-ask midpoint (for warrant-side recompute) AND the raw best
    level (for the underlying's own display row, see `_underlying_row_payload`)
    into `_underlying_books`. Assumes `_lock` is already held (safe re-entry —
    `_lock` is a `threading.RLock`).

    Falls back to the one side available when the book is one-sided, and
    leaves the last known price/book untouched when the tick carries neither
    side (a warrant's own next tick just uses whatever price is currently
    cached; the display row just keeps showing its last known level).
    """
    best = live_warrant_logic.best_level(bids, asks)
    bid, ask = best.get("bid"), best.get("ask")
    if bid is not None and ask is not None:
        price = (bid + ask) / 2
    elif bid is not None:
        price = bid
    elif ask is not None:
        price = ask
    else:
        return
    _underlying_books[code] = {
        "price": price, "best": best, "ts": datetime.now(timezone.utc), "src": "ws",
    }


def _handle_control(conn, event, message):
    """Track this connection's own subscription ids and errors — unsubscribe needs the id, not the symbol."""
    data = message.get("data") or {}
    with _lock:
        if event == "subscribed":
            conn["sub_ids"][data.get("symbol")] = data.get("id")
        elif event == "unsubscribed":
            conn["sub_ids"].pop(data.get("symbol"), None)
        elif event == "error":
            conn["last_error"] = f"{message.get('code', '?')}: {data.get('message', message)}"
            print(f"LIVEWARRANT: conn {conn.get('index', '?')} error {conn['last_error']}", flush=True)


def _on_connect(conn):
    with _lock:
        conn["state"] = "connected"
    print(f"LIVEWARRANT: conn {conn.get('index', '?')} connected", flush=True)


def _on_disconnect(conn):
    with _lock:
        was_connected = conn["state"] == "connected"
        conn["state"] = "disconnected"
    print(f"LIVEWARRANT: conn {conn.get('index', '?')} disconnected", flush=True)
    if was_connected:
        threading.Thread(target=_reconnect_connection, args=(conn,), daemon=True).start()


def _on_error(conn, args, kwargs):
    with _lock:
        conn["state"] = "error"
        conn["last_error"] = "; ".join(str(x) for x in args) or repr(kwargs)
    print(f"LIVEWARRANT: conn {conn.get('index', '?')} error {args!r} {kwargs!r}", flush=True)


def _reconnect_connection(conn):
    """A single connection's own auto-heal: re-login and resubscribe its own codes.

    Other connections are untouched — a transient blip on one socket must not
    disturb the rest of the pool. Gives up after RECONNECT_RETRIES and leaves
    the connection in "error" state for the manual Reconnect (whole-session
    restart) to recover.
    """
    idx = conn.get("index", "?")
    for attempt in range(1, RECONNECT_RETRIES + 1):
        with _lock:
            codes = set(conn["codes"])
        try:
            new_conn = _login_new_connection()
            with _lock:
                if conn not in _connections:
                    return  # torn down or already replaced while we were reconnecting
                new_conn["index"] = idx
                _connections[_connections.index(conn)] = new_conn
            for code in codes:
                new_conn["ws"].subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
                with _lock:
                    new_conn["codes"].add(code)
            print(f"LIVEWARRANT: conn {idx} reconnected, resubscribed {len(codes)} codes", flush=True)
            return
        except Exception as e:
            print(f"LIVEWARRANT: conn {idx} reconnect attempt {attempt} failed: {e}", flush=True)
            time.sleep(RECONNECT_BACKOFF_S * attempt)

    with _lock:
        conn["state"] = "error"
        conn["last_error"] = "auto-reconnect exhausted retries"
    print(f"LIVEWARRANT: conn {idx} auto-reconnect exhausted retries", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Per-code subscribe/unsubscribe
# ─────────────────────────────────────────────────────────────────────────────
def _seed_from_rest(sdk, code):
    """One REST quote per added code: fills its first book and backfills the name.

    The books channel is update-only (pushes on change, no snapshot on
    subscribe), so a book with no recent activity would stay blank until a
    market maker requotes without this.

    Failure is retried, then recorded in `_seed_errors` and left to
    retry_pending() — it never touches `_names` and never removes the code. The
    name is only a nicety: it also arrives in bulk from the ticker catalog on
    the scan path, and the live prices come over the websocket regardless.
    Returns True when the quote came back.
    """
    for attempt in range(1, SEED_RETRIES + 1):
        try:
            q = _rest_call(f"quote {code}",
                           sdk.marketdata.rest_client.stock.intraday.quote, symbol=code)
            break
        except Exception as e:
            detail = _error_detail(e)
            if attempt == SEED_RETRIES:
                with _lock:
                    _seed_errors[code] = detail
                print(f"LIVEWARRANT: seed {code} failed after {attempt} attempts: {detail}",
                      flush=True)
                return False
            # Linear backoff: the common failure is a per-second REST quota, so
            # waiting out the current second is usually enough.
            time.sleep(SEED_BACKOFF_S * attempt)

    with _lock:
        _seed_errors.pop(code, None)
        # Seeded means "the REST quote came back", not "the book had depth" — a
        # genuinely empty book must not be re-seeded on every tick forever.
        _seeded.add(code)
        if q.get("name"):
            _names[code] = q["name"]

    bids, asks = q.get("bids") or [], q.get("asks") or []
    if not bids and not asks:
        return True

    stamp = q.get("lastUpdated")
    try:
        ts = datetime.fromtimestamp(stamp / 1_000_000, timezone.utc)
    except (TypeError, ValueError, OSError):
        ts = datetime.now(timezone.utc)
    with _lock:
        _books.setdefault(code, {"bids": bids, "asks": asks, "ts": ts, "src": "rest"})
    return True


def _parse_maturity(raw):
    """Fugle's "20261016" -> a date. None for anything unparseable."""
    try:
        return datetime.strptime(str(raw), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _days_to_expiry(maturity):
    """Calendar days from today (Taipei) to maturity — negative once expired.

    Recomputed on every read rather than stored: the strike and the ratio are
    fixed for the warrant's life, but the DTE is only true for one day.
    """
    if maturity is None:
        return None
    return (maturity - datetime.now(TW_TZ).date()).days


def _nan_to_none(v):
    """A bare NaN literal from `jsonify` breaks `JSON.parse` client-side (see
    app.py's `_rows_json` docstring for the same trap on the Scanner path,
    worked around there via `to_json`'s NaN->null instead — this path has no
    dataframe to reach for that). `iv_engine.solve_tick` returns `np.nan` for
    a missing side."""
    try:
        if v != v:  # NaN is the only float that is not equal to itself
            return None
    except TypeError:
        pass
    return v


def _fetch_terms(sdk, code):
    """Strike, exercise ratio and maturity for one code, from intraday/ticker.

    intraday/quote — the call the book seed makes — carries none of these, so
    this is a separate round trip. Cached permanently on success; a failure is
    left uncached so the next retry_pending() pass picks it up again, and is
    deliberately NOT written to `_seed_errors`, which tracks the streaming path.
    """
    try:
        t = _rest_call(f"ticker {code}",
                       sdk.marketdata.rest_client.stock.intraday.ticker, symbol=code) or {}
    except Exception as e:
        print(f"LIVEWARRANT: terms {code} failed: {_error_detail(e)}", flush=True)
        return False

    def num(key):
        try:
            v = float(t.get(key))
        except (TypeError, ValueError):
            return None
        return v or None  # 0 means "not applicable" in this payload, not a real 0

    with _lock:
        _terms[code] = {
            "strike": num("exercisePrice"),
            "exercise_ratio": num("exerciseRatio"),
            "maturity": _parse_maturity(t.get("maturityDate")),
        }
        if t.get("name"):
            _names.setdefault(code, t["name"])
    return True


def _subscribe_one(code):
    """Place one code on whichever pool connection has room, opening one if needed.

    Slot assignment stays single-threaded on purpose: `assign_slot` reads the
    per-connection counts and the caller then fills that slot, so running two of
    these concurrently could hand the same slot out twice and overfill a
    connection. The websocket subscribe itself is a local send with no
    round-trip, so serialising this costs almost nothing even for a whole chain.
    """
    with _lock:
        counts = [len(c["codes"]) for c in _connections]
    idx = live_warrant_logic.assign_slot(counts)
    conn = _ensure_connection(idx)
    conn["ws"].subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
    with _lock:
        conn["codes"].add(code)
    return conn


def _track_one(code):
    """Subscribe one code and seed its first book.

    Raises if the subscribe itself fails (the caller decides what that means);
    a failed REST seed is recorded, not raised.
    """
    conn = _subscribe_one(code)
    with _lock:
        _pending.discard(code)
    _seed_from_rest(conn["sdk"], code)


def _subscribe_underlying(code, name=None):
    """Idempotent: subscribe one underlying stock to the same books channel
    the warrant codes use, for a live tick-driven price AND its own display
    row (see `_fold_underlying_tick_locked` / `_underlying_row_payload`).
    Reuses the existing connection pool exactly as a warrant subscribe does —
    capacity is negligible, one extra subscription per allow-listed
    underlying against the 2,100 cap.

    Called before any of that underlying's warrant codes are subscribed (see
    `scan_underlying`), so the underlying's row appears — and starts filling
    in — before its warrant chain does.

    Non-fatal on failure, same pattern as `_track_many`'s per-code handling:
    an underlying that fails to subscribe just means that underlying's
    warrants keep degrading to "—" on the price-dependent columns, not that
    the whole session breaks.
    """
    with _lock:
        if name:
            _underlying_names[code] = name
        if code in _underlying_codes:
            return
        _underlying_codes.add(code)
    try:
        _subscribe_one(code)
    except Exception as e:
        with _lock:
            _underlying_codes.discard(code)
        print(f"LIVEWARRANT: underlying subscribe {code} failed: {_error_detail(e)}", flush=True)


def _track_many(codes, seed_budget=None):
    """Subscribe a batch, then seed the books in parallel.

    The REST seed is one round-trip per code and is the whole cost of a large
    scan — a full TSMC chain is ~1,050 of them, which sequentially overruns
    gunicorn's request timeout and leaves the scan half-applied. Subscribing is
    still serial (see `_subscribe_one`); only the seeding fans out.

    A per-code subscribe failure is collected and returned, never raised: this
    used to abort the whole batch, which left the codes it had already reached
    subscribed-but-unpersisted and the codes it never reached missing outright.
    Anything that fails here stays in `_pending` and is picked up by
    retry_pending() on the next scheduler tick.
    """
    conns, failed = [], []
    for code in codes:
        try:
            conns.append((code, _subscribe_one(code)))
        except Exception as e:
            detail = _error_detail(e)
            failed.append(code)
            with _lock:
                _pending.add(code)
                _seed_errors[code] = f"subscribe failed: {detail}"
            print(f"LIVEWARRANT: subscribe {code} failed: {detail}", flush=True)
    with _lock:
        for code, _conn in conns:
            _pending.discard(code)
    # Seeding is quota-bound (REST_QUOTA/minute), so a whole-chain scan cannot
    # seed everything inside one web request. Spend the budget, leave the rest
    # unseeded — those codes are subscribed and will fill from the websocket on
    # the first requote, and retry_pending() seeds them at leisure.
    to_seed = conns if seed_budget is None else conns[:seed_budget]
    if to_seed:
        with ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
            list(pool.map(lambda pair: _seed_from_rest(pair[1]["sdk"], pair[0]), to_seed))
    if len(to_seed) < len(conns):
        print(f"LIVEWARRANT: seeded {len(to_seed)}/{len(conns)} inline, "
              f"{len(conns) - len(to_seed)} left for retry_pending", flush=True)
    return failed


def _untrack_one(code):
    """Unsubscribe one code from whichever connection holds it.

    The diagnostics state is cleared first: a code that never got subscribed at
    all is only ever in `_pending`, so clearing it after the `conn is None`
    early-return would leak it into the pending count forever.
    """
    with _lock:
        _pending.discard(code)
        _seeded.discard(code)
        _terms.pop(code, None)
        _seed_errors.pop(code, None)
        _book_seq.pop(code, None)
        _computed.pop(code, None)
        _pending_log.pop(code, None)
        _volumes.pop(code, None)
        _underlying_of.pop(code, None)
        # A code can also be an underlying's own subscription (see
        # _subscribe_underlying) — clear that side too so a manual Remove on
        # its display row doesn't leave it half-torn-down.
        _underlying_codes.discard(code)
        _underlying_books.pop(code, None)
        _underlying_names.pop(code, None)
        conn = next((c for c in _connections if code in c["codes"]), None)
        sub_id = conn["sub_ids"].get(code) if conn else None
    if conn is None:
        return
    try:
        if sub_id:
            conn["ws"].unsubscribe({"id": sub_id})
    except Exception as e:
        print(f"LIVEWARRANT: unsubscribe {code} failed: {e}", flush=True)
    with _lock:
        conn["codes"].discard(code)
        conn["sub_ids"].pop(code, None)
        _books.pop(code, None)


# ─────────────────────────────────────────────────────────────────────────────
# Session lifecycle (called by the scheduler gate and the manual Reconnect route)
# ─────────────────────────────────────────────────────────────────────────────
def start_session():
    """Idempotent: no-op if the pool already has connections or the user disconnected; otherwise loads the persisted tracked list."""
    global _session_error
    with _lock:
        if _connections or _stopped_by_user:
            return
    _session_error = None

    try:
        rows = db_live_warrant.list_tracked()
    except Exception as e:
        _session_error = f"failed to load tracked list: {type(e).__name__}: {e}"
        print(f"LIVEWARRANT: {_session_error}", flush=True)
        return

    with _lock:
        _tracked.clear()
        _pending.clear()
        _seeded.clear()
        _seed_errors.clear()
        _underlying_of.clear()
        # `_terms` deliberately survives a restart of the session: a warrant's
        # strike, ratio and maturity never change, and re-fetching them would
        # spend a full quota window on data we already have.
    for row in rows:
        code = row["code"]
        stored = row.get("name")
        usable = bool(stored) and not _is_placeholder_name(stored)
        with _lock:
            if usable:
                _names[code] = stored
            _tracked.append(code)
            _pending.add(code)
            _underlying_of[code] = row.get("underlying")
        try:
            _track_one(code)
            # The seed above may have produced the first real name for a row
            # written before it was subscribed, or for one still carrying an
            # old "(FugleAPIError)" placeholder. Write it back so the pollution
            # clears on its own rather than being re-read every restart.
            with _lock:
                fresh = _names.get(code)
            if fresh and fresh != stored:
                try:
                    db_live_warrant.update_name(code, fresh)
                except Exception as db_e:
                    print(f"LIVEWARRANT: name backfill {code} failed: {db_e}", flush=True)
        except Exception as e:
            detail = _error_detail(e)
            _session_error = f"{code}: {detail}"
            with _lock:
                _seed_errors[code] = detail
            print(f"LIVEWARRANT: track {code} failed: {detail}", flush=True)

    # Underlying-price subscriptions are not persisted anywhere — re-derived
    # from the tracked list's own `underlying` column every (re)start, same as
    # the warrant subscriptions themselves.
    with _lock:
        underlyings = {u for u in _underlying_of.values() if u in UNDERLYING_LIVE_PRICE_ALLOWLIST}
    for underlying in underlyings:
        _subscribe_underlying(underlying)


def retry_pending(limit=PENDING_RETRY_BATCH):
    """Re-attempt whatever the last scan/add could not finish.

    This is what makes a throttled scan lossless rather than merely survivable:
    every code is written to live_warrant_tracked before it is subscribed, so a
    failure only ever leaves the code *pending*, and this pass — driven by the
    scheduler's trading-hour tick — keeps working through the backlog until the
    subscription and the name are both there. Nothing here is destructive; a
    code is only dropped by an explicit remove or a scan replace.

    Also backfills the contract terms (strike / exercise ratio / maturity) the
    table shows, a bounded batch at a time — those are one REST call per code
    and never change, so the work runs down to nothing. And refreshes the
    Live Warrant tab's Volume column via `_mis_volumes()` for the whole
    tracked list every call — safe to do every tick since that call goes
    straight to TWSE MIS (`requests.get`), bypassing the Fugle REST quota
    entirely, unlike everything else in this function.

    Returns {"subscribed", "reseeded", "terms", "pending", "terms_missing"}.
    """
    with _lock:
        if _stopped_by_user or not _connections:
            return {"subscribed": 0, "reseeded": 0, "pending": len(_pending)}
        todo = [c for c in _tracked if c in _pending][:limit]
        # Subscribed but never successfully seeded — either the scan spent its
        # inline budget before reaching it, or its quote failed. Both want the
        # same thing: one more REST quote, paced by the quota.
        reseed = [c for c in _tracked
                  if c not in _pending and c not in _seeded][:limit]
        # Terms are one-shot per code and never expire, so this list empties for
        # good once every tracked warrant has been looked up.
        need_terms = [c for c in _tracked if c not in _terms][:TERMS_RETRY_BATCH]
        volume_batch = list(_tracked)[:VOLUME_RETRY_BATCH]

    if volume_batch:
        vols, _missing = _mis_volumes(volume_batch)
        with _lock:
            _volumes.update(vols)

    subscribed = 0
    for code in todo:
        try:
            _track_one(code)
            subscribed += 1
        except Exception as e:
            detail = _error_detail(e)
            with _lock:
                _seed_errors[code] = f"subscribe failed: {detail}"
            print(f"LIVEWARRANT: retry subscribe {code} failed: {detail}", flush=True)

    reseeded = 0
    for code in reseed:
        with _lock:
            conn = next((c for c in _connections if code in c["codes"]), None)
        if conn and _seed_from_rest(conn["sdk"], code):
            reseeded += 1
            with _lock:
                name = _names.get(code)
            if name:
                try:
                    db_live_warrant.update_name(code, name)
                except Exception as e:
                    print(f"LIVEWARRANT: name backfill {code} failed: {e}", flush=True)

    termed = 0
    for code in need_terms:
        with _lock:
            conn = _connections[0] if _connections else None
        if conn and _fetch_terms(conn["sdk"], code):
            termed += 1

    with _lock:
        still = len(_pending)
        missing_terms = sum(1 for c in _tracked if c not in _terms)
    if subscribed or reseeded or termed:
        print(f"LIVEWARRANT: retry_pending subscribed={subscribed} reseeded={reseeded} "
              f"terms={termed} pending={still} terms_missing={missing_terms}", flush=True)
    return {"subscribed": subscribed, "reseeded": reseeded, "terms": termed,
            "pending": still, "terms_missing": missing_terms}


def _teardown():
    """Close every pooled connection and log out. `state` is set to "closing" before
    disconnect() so _on_disconnect doesn't see was_connected=True and spawn an
    auto-reconnect thread that would log in a fresh, never-logged-out session."""
    with _lock:
        conns = list(_connections)
        _connections.clear()
        for conn in conns:
            conn["state"] = "closing"
        # Nothing is subscribed any more, so every tracked code is pending again.
        # The rows themselves are untouched — a teardown never drops a code.
        _pending.update(_tracked)
    for conn in conns:
        try:
            conn["ws"].disconnect()
        except Exception as e:
            print(f"LIVEWARRANT: teardown disconnect failed: {e}", flush=True)
        try:
            conn["sdk"].logout()
        except Exception as e:
            print(f"LIVEWARRANT: teardown logout failed: {e}", flush=True)


def reconnect():
    """Manual hard restart: tear down the whole pool, then reopen from the persisted tracked list."""
    global _stopped_by_user
    _stopped_by_user = False
    _teardown()
    start_session()


def connect_session():
    """Manual Connect: clear the user-stop flag, then start_session()."""
    global _stopped_by_user
    _stopped_by_user = False
    start_session()


def stop_session():
    """Manual disconnect: tear down the whole pool and stay stopped until connect_session(); also used by wsgi.py's shutdown hook."""
    global _stopped_by_user
    _stopped_by_user = True
    _teardown()


# ─────────────────────────────────────────────────────────────────────────────
# Tracked-list mutation (persist, then subscribe/unsubscribe)
# ─────────────────────────────────────────────────────────────────────────────
def add_code(code):
    """Manual add. No-op if already tracked; raises CapacityExceededError at the account cap."""
    with _lock:
        existing = set(_tracked)
        current_total = len(_tracked)
    if not live_warrant_logic.plan_manual_add(existing, code, current_total):
        return

    # Persist first, subscribe second. The row is the record of what the user
    # asked for; the subscription is best-effort and retried by retry_pending().
    # Doing it the other way round meant a throttled or failed subscribe left
    # nothing behind at all.
    with _lock:
        _tracked.append(code)
        _pending.add(code)
    db_live_warrant.upsert_tracked(code, _display_name(code), source="manual")

    try:
        _track_one(code)
    except Exception as e:
        detail = _error_detail(e)
        with _lock:
            _seed_errors[code] = f"subscribe failed: {detail}"
        print(f"LIVEWARRANT: add {code} subscribe failed: {detail}", flush=True)
        return

    with _lock:
        name = _names.get(code)
    if name:
        db_live_warrant.update_name(code, name)


def remove_code(code):
    db_live_warrant.remove_tracked(code)
    with _lock:
        if code in _tracked:
            _tracked.remove(code)
        _names.pop(code, None)
    _untrack_one(code)


def _remove_many(codes):
    """Drop a batch of codes: one bulk delete, then unsubscribe each."""
    if not codes:
        return
    db_live_warrant.remove_tracked_many(codes)
    with _lock:
        for code in codes:
            if code in _tracked:
                _tracked.remove(code)
            _names.pop(code, None)
    for code in codes:
        _untrack_one(code)


def scan_underlying(underlying, top_n, force=False):
    """Rank `underlying`'s warrants by traded volume and replace its previous
    scan rows with the top N — or with the WHOLE chain when `top_n` is 0.

    The bulk paths (batched upsert, parallel seeding) are what make the
    full-chain case finish inside one request; the account-wide subscription cap
    is still enforced by `plan_scan_replace` before anything is subscribed, so an
    oversized scan is rejected outright rather than partially applied.

    Nothing here can lose a code. The additions are persisted before any
    subscribe is attempted and before any removal, per-code subscribe failures
    are collected rather than raised, and `guard_chain_shrink` refuses the
    destructive half outright when the resolved chain is suspiciously smaller
    than what is already tracked (`force=True` overrides that, and is the only
    way past it).
    """
    chain = _warrant_codes_for(underlying)
    codes, catalog = chain["codes"], chain["names"]
    if not codes:
        raise RuntimeError(f"no warrants found for {underlying}")

    if underlying in UNDERLYING_LIVE_PRICE_ALLOWLIST:
        _subscribe_underlying(underlying, name=chain.get("name"))

    vols, vol_missing = _mis_volumes(codes)
    ranked_codes = live_warrant_logic.scan_codes(codes, vols, top_n)

    existing_rows = db_live_warrant.list_tracked()
    with _lock:
        current_total = len(_tracked)
    to_add, to_remove = live_warrant_logic.plan_scan_replace(
        existing_rows, underlying, ranked_codes, current_total)
    shrink = live_warrant_logic.guard_chain_shrink(
        existing_rows, underlying, ranked_codes, top_n,
        catalog_complete=chain["complete"], force=force)

    # Persist the additions BEFORE subscribing and BEFORE removing anything.
    # The old order (remove, subscribe, then persist) lost codes outright: a
    # subscribe that threw halfway left the deletions already committed and the
    # additions never written. Now the tracked table is always a superset of
    # what is live, and the subscriptions catch up.
    rows = []
    with _lock:
        for code in to_add:
            if catalog.get(code):
                _names[code] = catalog[code]
            if code not in _tracked:
                _tracked.append(code)
            _pending.add(code)
            _underlying_of[code] = underlying
            rows.append({"code": code, "name": _display_name(code),
                         "source": "scan", "underlying": underlying})
    db_live_warrant.upsert_tracked_many(rows)

    failed = _track_many(to_add, seed_budget=SEED_BUDGET_PER_SCAN)
    _remove_many(to_remove)

    with _lock:
        tracked_now, pending_now = len(_tracked), len(_pending)
    print(f"LIVEWARRANT: scan {underlying} top_n={top_n or 'all'} chain={len(codes)} "
          f"+{len(to_add)} -{len(to_remove)} failed={len(failed)} "
          f"tracked={tracked_now} pending={pending_now} "
          f"catalog_complete={chain['complete']} vol_missing={len(vol_missing)}", flush=True)
    return {
        "added": to_add,
        "removed": to_remove,
        "failed": failed,
        "chain": len(codes),
        "catalog_complete": chain["complete"],
        "volume_missing": len(vol_missing),
        "shrink": round(shrink, 4),
        "pending": pending_now,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity ranking (adapted from scripts/fubon_quote_viewer.py)
# ─────────────────────────────────────────────────────────────────────────────
def _rest_client():
    """REST client for liquidity-scan queries — lazily opens connection 0 if the
    pool is still empty (e.g. the very first scan, before anything is tracked)."""
    conn = _ensure_connection(0)
    return conn["sdk"].marketdata.rest_client.stock


def _tickers(rest, **params):
    """One ticker-catalog page, as (rows, ok).

    `ok` is False when the call failed or answered with nothing. The catalog is
    the only thing that decides which codes exist, so a truncated answer is not
    an empty chain — the caller has to be able to tell the two apart before it
    lets a scan delete anything.
    """
    try:
        rows = (_rest_call(f"tickers {params}", rest.intraday.tickers, **params) or {}).get("data") or []
    except Exception as e:
        print(f"LIVEWARRANT: ticker catalog {params} failed: {_error_detail(e)}", flush=True)
        return [], False
    if not rows:
        print(f"LIVEWARRANT: ticker catalog {params} came back empty", flush=True)
        return [], False
    return rows, True


def _warrant_codes_for(stock_code):
    """Every listed warrant on one underlying, matched by name (see the fuller
    docstring on the original in scripts/fubon_quote_viewer.py::_warrant_codes_for
    for why this can't be a plain prefix test).

    Returns {"name", "codes", "names", "complete"}. `names` is the catalog's own
    symbol -> name map, which is what lets the scan path label every warrant
    without a single per-code REST quote — the call that gets throttled in bulk
    and used to leave "(FugleAPIError)" as the instrument's name. `complete` is
    False when any catalog page failed or came back empty, and gates the
    destructive half of a scan.
    """
    rest = _rest_client()
    name = (_rest_call(f"ticker {stock_code}", rest.intraday.ticker, symbol=stock_code)
            or {}).get("name")
    if not name:
        return {"name": name, "codes": [], "names": {}, "complete": False}

    complete = True
    warrants = []
    for market in WARRANT_MARKETS:
        rows, ok = _tickers(rest, type="WARRANT", market=market)
        complete = complete and ok
        warrants.extend(rows)
    if not warrants:
        raise RuntimeError(
            f"warrant catalog came back empty for every market — refusing to scan "
            f"{stock_code} against nothing")

    real = []
    for market in ("TSE", "OTC"):
        rows, ok = _tickers(rest, type="EQUITY", market=market)
        complete = complete and ok
        real.extend(r.get("name") or "" for r in rows)

    longer = [n for n in real if len(n) > len(name) and n.startswith(name)]
    matched = [w for w in warrants
               if (w.get("name") or "").startswith(name)
               and not any((w.get("name") or "").startswith(n) for n in longer)]
    codes = [w["symbol"] for w in matched]
    names = {w["symbol"]: w.get("name") for w in matched if w.get("name")}
    return {"name": name, "codes": codes, "names": names, "complete": complete}


def _mis_volumes(codes):
    """Accumulated traded volume (張) per code, from TWSE MIS in bulk.

    Returns (volumes, missing). A code MIS never answered for and a code that
    genuinely traded zero are indistinguishable once they are both absent from
    the ranking, so a dropped batch would quietly demote real warrants out of a
    top-N scan. Batches are retried, and whatever is still missing is reported
    so the caller can say so instead of silently ranking on a hole.
    """
    batches = [codes[i:i + MIS_BATCH] for i in range(0, len(codes), MIS_BATCH)]

    def one(batch):
        for attempt in range(1, MIS_RETRIES + 1):
            try:
                r = requests.get(MIS_URL, timeout=15, headers=MIS_HEADERS, params={
                    "ex_ch": "|".join(f"tse_{c}.tw" for c in batch),
                    "json": "1", "delay": "0"})
                rows = r.json().get("msgArray") or []
                if rows:
                    return rows
            except Exception as e:
                if attempt == MIS_RETRIES:
                    print(f"LIVEWARRANT: MIS batch failed after {attempt} attempts: {e}",
                          flush=True)
                    return []
            time.sleep(MIS_BACKOFF_S * attempt)
        return []

    out = {}
    with ThreadPoolExecutor(max_workers=MIS_WORKERS) as pool:
        for rows in pool.map(one, batches):
            for row in rows:
                try:
                    out[row.get("c")] = int(row.get("v") or 0)
                except (TypeError, ValueError):
                    continue

    missing = [c for c in codes if c not in out]
    if missing:
        print(f"LIVEWARRANT: MIS answered for {len(out)}/{len(codes)} codes — "
              f"{len(missing)} ranked without volume", flush=True)
    return out, missing


# ─────────────────────────────────────────────────────────────────────────────
# Read path
# ─────────────────────────────────────────────────────────────────────────────
def get_data():
    """The cache as a JSON-able dict — what the /data route serves."""
    with _lock:
        conn_snapshot = [
            {"index": i, "state": c["state"], "subs": len(c["codes"]), "last_error": c["last_error"]}
            for i, c in enumerate(_connections)
        ]
        tracked_snapshot = list(_tracked)
        underlying_snapshot = sorted(_underlying_codes)
        pending_count = len(_pending)
        # The distinct failure strings, not one per code: a throttled scan
        # produces the same message a thousand times over.
        error_kinds = sorted(set(_seed_errors.values()))
        error_count = len(_seed_errors)
        unseeded = sum(1 for c in tracked_snapshot if c not in _seeded)
        terms_missing = sum(1 for c in tracked_snapshot if c not in _terms)
        console_log_id = _console_log_next_id
    return {
        "connected": any(c["state"] == "connected" for c in conn_snapshot),
        "session_error": _session_error,
        "connections": conn_snapshot,
        "subs": sum(c["subs"] for c in conn_snapshot),
        "max_subs": live_warrant_logic.MAX_TOTAL_SUBS,
        "max_connections": live_warrant_logic.MAX_CONNECTIONS,
        "tracked": len(tracked_snapshot),
        "pending": pending_count,
        "unseeded": unseeded,
        "terms_missing": terms_missing,
        "quota_used": _rest_quota.used(),
        "quota_limit": REST_QUOTA,
        "errors": error_count,
        "error_kinds": error_kinds[:5],
        "console_log_id": console_log_id,
        # Underlying rows first — an underlying is subscribed before its
        # warrant chain (see scan_underlying), so its row appears and starts
        # filling in while the chain is still loading, rather than being
        # buried wherever it'd fall in tracked-add order.
        "books": [_underlying_row_payload(code) for code in underlying_snapshot]
                 + [_ladder_payload(code) for code in tracked_snapshot],
    }


def _underlying_row_payload(code):
    """One live-priced underlying's own display row — kept entirely separate
    from `_tracked`/`_ladder_payload` so an underlying never touches warrant-
    only accounting (capacity, terms backfill, MIS volume ranking, DB
    persistence). Every warrant-only field comes back None/"—"; the row only
    ever carries a name, its own best bid/ask, and the price warrants read
    from `_underlying_books`.
    """
    with _lock:
        info = _underlying_books.get(code)
        name = _underlying_names.get(code) or code
    if info is None:
        return {
            "code": code, "name": name, "pending": True, "error": None,
            "type": "Underlying", "underlying_code": code, "underlying_price": None,
            "strike": None, "exercise_ratio": None, "dte": None, "volume": None,
            "time_value": None, "bid_time_value_pct": None, "ask_time_value_pct": None,
            "best": live_warrant_logic.best_level([], []),
            "age": None, "src": None,
        }
    return {
        "code": code, "name": name, "pending": False, "error": None,
        "type": "Underlying", "underlying_code": code, "underlying_price": info.get("price"),
        "strike": None, "exercise_ratio": None, "dte": None, "volume": None,
        "time_value": None, "bid_time_value_pct": None, "ask_time_value_pct": None,
        "best": info.get("best") or live_warrant_logic.best_level([], []),
        "age": round((datetime.now(timezone.utc) - info["ts"]).total_seconds(), 1),
        "src": info.get("src"),
    }


def _recompute_if_dirty(code):
    """Recompute one code's derived (tick-kernel) columns, but only when its
    best level has actually moved since the last computed value — the gate
    that keeps an illiquid warrant's columns from being recomputed on every
    500ms poll. Missing inputs (terms not backfilled yet, underlying not
    priced yet — only TSMC is live-priced today, expired) leave whatever is
    cached untouched, same degrade-to-"—" pattern the tab already uses for
    strike/DTE; the queued console-log diffs stay pending until the inputs
    do show up, rather than being dropped.
    """
    with _lock:
        seq = _book_seq.get(code)
        if seq is None:
            return  # never ticked yet
        cached = _computed.get(code)
        if cached is not None and cached["seq"] == seq:
            return  # already current

        book = _books.get(code)
        terms = _terms.get(code) or {}
        strike = terms.get("strike")
        ratio = terms.get("exercise_ratio")
        dte = _days_to_expiry(terms.get("maturity"))
        underlying = _underlying_of.get(code)
        underlying_price = (
            _underlying_books.get(underlying, {}).get("price") if underlying else None
        )
        type_label = live_warrant_logic.parse_warrant_type(_display_name(code))

        if (book is None or strike is None or ratio is None or underlying_price is None
                or type_label is None or (dte is not None and dte <= 0)):
            return

        bids, asks = book["bids"], book["asks"]
        pending_diffs = list(_pending_log.get(code) or [])
        _pending_log.pop(code, None)

    best = live_warrant_logic.best_level(bids, asks)
    bid, ask = best.get("bid") or 0.0, best.get("ask") or 0.0
    is_put = type_label == "Put"

    t0 = time.perf_counter()
    time_value, bid_pct, ask_pct = iv_engine.solve_tick(
        underlying_price, strike, ratio, is_put, bid, ask)
    duration_s = round(time.perf_counter() - t0, 4)

    global _console_log_next_id
    with _lock:
        _computed[code] = {
            "seq": seq,
            # NaN -> None: a bare NaN literal from jsonify breaks JSON.parse
            # (the same trap /read_warrant works around with to_json instead
            # of jsonify — this path has no dataframe to reach for that, so
            # it sanitizes by hand).
            "time_value": _nan_to_none(time_value),
            "bid_time_value_pct": _nan_to_none(bid_pct),
            "ask_time_value_pct": _nan_to_none(ask_pct),
        }
        for entry in pending_diffs:
            _console_log_next_id += 1
            _console_log.append({
                "id": _console_log_next_id,
                "ts": entry["ts"].isoformat(),
                "code": code,
                "diff": entry["diff"],
                "recalculated": "time_value, bid_time_value_pct, ask_time_value_pct",
                "duration_s": duration_s,
            })


def get_console_log(since=0, limit=200):
    """Book-change log entries with id > `since`, oldest first, capped at `limit`."""
    with _lock:
        entries = [e for e in _console_log if e["id"] > since][-limit:]
        latest_id = _console_log_next_id
    return {"entries": entries, "latest_id": latest_id}


def _ladder_payload(code):
    """One tracked code's row.

    `pending` and `error` are carried per code so a degraded row is visibly
    degraded — the name column shows the code, and the reason lives in its own
    field. Writing the reason into the name is what produced "(FugleAPIError)".
    """
    _recompute_if_dirty(code)
    with _lock:
        book = _books.get(code)
        name = _display_name(code)
        pending = code in _pending
        error = _seed_errors.get(code)
        terms = _terms.get(code) or {}
        underlying = _underlying_of.get(code)
        underlying_price = (
            _underlying_books.get(underlying, {}).get("price") if underlying else None
        )
        computed = _computed.get(code) or {}
        volume = _volumes.get(code)
    base = {
        "code": code, "name": name, "pending": pending, "error": error,
        "type": live_warrant_logic.parse_warrant_type(name),
        "underlying_code": underlying,
        "underlying_price": underlying_price,
        "strike": terms.get("strike"),
        "exercise_ratio": terms.get("exercise_ratio"),
        "dte": _days_to_expiry(terms.get("maturity")),
        "volume": volume,
        "time_value": computed.get("time_value"),
        "bid_time_value_pct": computed.get("bid_time_value_pct"),
        "ask_time_value_pct": computed.get("ask_time_value_pct"),
    }
    if book is None:
        return {**base, "best": live_warrant_logic.best_level([], []), "age": None, "src": None}
    return {
        **base,
        "best": live_warrant_logic.best_level(book["bids"], book["asks"]),
        "age": round((datetime.now(timezone.utc) - book["ts"]).total_seconds(), 1),
        "src": book.get("src"),
    }
