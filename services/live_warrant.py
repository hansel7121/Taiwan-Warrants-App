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
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from logic import iv_engine, live_warrant_logic
from services import db_live_warrant, live_tick_log, memlog
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
# The 300/60s cap alone only bounds the AVERAGE rate: a fresh window (e.g.
# right after boot) lets many threads see room and fire in the same instant.
# A first-attempt fix paced EVERY grant to the full-account average (5/s,
# window/quota) — that turned out far more conservative than the account
# actually needs: both this account's observed bursts (8 workers, no pacing
# at all) moved ~200 codes in ~11s with zero errors; the real 429s only
# showed up later, once cumulative volume was already near the 300 ceiling.
# Flattening every call to 5/s made a 200-call stage take ~40s instead of
# ~11s for no measured benefit over just capping the BURST rate instead.
# This second, much shorter window is what actually addresses the failure:
# it caps how many can be granted within any single second (comfortably
# above what SEED_WORKERS's natural 8-way concurrency needs, even with two
# fan-outs running at once per retry_pending()'s concurrent reseed+terms),
# without slowing the account down to its raw 60s average the way pacing
# every single grant did.
REST_BURST_LIMIT = 24
REST_BURST_WINDOW_S = 1.0
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
#
# Sized to REST_QUOTA rather than some smaller round number: _fan_out already
# refuses to run this list past retry_pending()'s own RETRY_PENDING_BUDGET_S
# deadline, and every call still goes through the shared REST quota, so a
# lower cap here does not make one retry_pending() call any safer — it just
# means fewer terms get fetched than the budget/quota would actually allow.
# At the old cap of 60, backfilling a full ~1,050-code chain's strike/DTE/
# ratio took ~18 rounds of the scheduler's 5-minute retry_pending() tick
# (~90 minutes) even though a single call, given the room, could clear most
# of that chain in one pass.
TERMS_RETRY_BATCH = REST_QUOTA
TW_TZ = ZoneInfo("Asia/Taipei")
MIS_RETRIES = 3
MIS_BACKOFF_S = 1.0
# Codes re-attempted per retry_pending() call. Bounded so a scheduler tick that
# inherits a fully failed 1,000-code scan still finishes promptly.
PENDING_RETRY_BATCH = 100
# Hard wall-clock ceiling on one retry_pending() call, independent of how
# many codes are queued or how contended the REST quota is. Parallelizing
# the three REST-bound stages (see _fan_out) bounds how many codes run at
# once, not how long the call can take — pool.map() still blocks until
# every submitted call finishes, and one quota-starved call can take up to
# SEED_RETRIES*(REST_ACQUIRE_TIMEOUT_S+backoff) ≈ 63s on its own. A big
# backlog (e.g. right after a redeploy) could make even the parallelized
# version exceed gunicorn's request timeout and get killed mid-batch — the
# exact "click Retry Pending, it fills a few rows then stops" symptom.
# Past this budget, whatever hasn't finished is simply left running in the
# background (its own thread still completes and updates state under _lock
# whenever it's done) rather than holding the HTTP response open.
RETRY_PENDING_BUDGET_S = 60
# A single REST call taking longer than this gets a debug checkpoint logged
# (see _log_debug) — the common fast case (a healthy call finishing in well
# under a second) stays quiet so the console log doesn't flood under a big
# batch; a call that's actually struggling shows up immediately instead of
# only being inferable after the fact from a missing "done" checkpoint.
SLOW_CALL_LOG_THRESHOLD_S = 3.0
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
_underlying_of = {}    # code -> underlying stock code, mirrors live_warrant_tracked.underlying
_underlying_books = {}  # underlying code -> {"price", "best", "ts", "src"}
_underlying_codes = set()  # underlying codes currently subscribed on the same connection pool
_underlying_names = {}  # underlying code -> display name, from the same ticker lookup a scan already makes
# Debug checkpoints only (see _log_debug) — per-tick book-change diffs used to
# be logged here too, but a busy chain produces far more of those than any
# freeze-diagnosis session can read, and they were pushing genuine checkpoints
# out of this deque's fixed size right when a freeze made them matter most.
_console_log = collections.deque(maxlen=1000)
_console_log_next_id = 0
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
    """Two sliding windows in front of every Fugle REST call: the account's
    60s budget, and a much shorter burst window layered on top of it.

    The SDK does nothing to pace requests, so the seed fan-out used to find the
    quota by hitting it: the first ~300 calls succeeded and every one after that
    came back 429 until the window rolled. Waiting for a slot turns that into
    latency instead of into a wall of failed seeds — which is what was showing
    up in the name column.

    Sliding windows rather than a token bucket because that is what the server
    enforces: the budget refills as individual requests age out, not in one
    lump. The burst window exists because the 60s window alone only bounds the
    AVERAGE rate — a fresh window lets many threads see room and fire in the
    same instant, and 60s later those same simultaneous hits age out and free
    a burst of slots all over again. The short window caps how many can be
    granted within any single second, without capping the sustained rate down
    to the account's raw 60s average the way pacing every grant to that
    average would (measured: it turned an ~11s stage into ~40s for no benefit
    over just capping the burst).
    """

    def __init__(self, limit=REST_QUOTA, window=REST_QUOTA_WINDOW_S,
                 burst_limit=REST_BURST_LIMIT, burst_window=REST_BURST_WINDOW_S):
        self._limit = limit
        self._window = window
        self._burst_limit = burst_limit
        self._burst_window = burst_window
        self._hits = collections.deque()
        self._lock = threading.Lock()

    def _burst_count_and_oldest(self, now):
        """How many of the current hits fall inside the burst window, and the
        oldest of those — `_hits` is ordered oldest-first, so scanning from
        the right (newest) stops as soon as one falls outside the window."""
        count, oldest = 0, None
        for t in reversed(self._hits):
            if now - t >= self._burst_window:
                break
            count += 1
            oldest = t
        return count, oldest

    def acquire(self, timeout=None):
        """Claim one request slot. False if `timeout` elapsed without one."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= self._window:
                    self._hits.popleft()
                burst_count, oldest_in_burst = self._burst_count_and_oldest(now)
                has_slot = len(self._hits) < self._limit
                has_burst_room = burst_count < self._burst_limit
                if has_slot and has_burst_room:
                    self._hits.append(now)
                    return True
                wait_slot = 0.0 if has_slot else self._window - (now - self._hits[0])
                wait_burst = (0.0 if has_burst_room
                              else self._burst_window - (now - oldest_in_burst))
                wait = max(wait_slot, wait_burst)
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
    _log_debug("login: fetching credentials from Supabase…")
    t0 = time.perf_counter()
    creds = broker_credentials.get_credential(FUBON_CRED_LABEL)
    _log_debug(f"login: credentials fetched in {time.perf_counter() - t0:.1f}s")
    if creds is None:
        raise RuntimeError(f"no fubon_credentials row for label '{FUBON_CRED_LABEL}'")
    missing = [k for k in ("fubon_id", "fubon_password", "cert_password") if not creds.get(k)]
    if missing:
        raise RuntimeError(f"fubon_credentials row '{FUBON_CRED_LABEL}' missing: {', '.join(missing)}")
    if not creds.get("cert_path"):
        raise RuntimeError(f"fubon_credentials row '{FUBON_CRED_LABEL}' has no cert uploaded")

    _log_debug("login: downloading cert from Supabase storage…")
    t0 = time.perf_counter()
    cert_bytes = broker_credentials.download_cert(FUBON_CRED_LABEL)
    _log_debug(f"login: cert downloaded in {time.perf_counter() - t0:.1f}s")

    from fubon_neo.sdk import FubonSDK

    cert_ext = creds["cert_path"].rsplit(".", 1)[-1] if "." in creds["cert_path"] else "p12"
    fd, local_cert_path = tempfile.mkstemp(suffix=f".{cert_ext}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cert_bytes)

        sdk = FubonSDK()
        _log_debug("login: calling sdk.login() (Fubon broker auth, no client-side timeout)…")
        t0 = time.perf_counter()
        result = sdk.login(creds["fubon_id"], creds["fubon_password"],
                           local_cert_path, creds["cert_password"])
        _log_debug(f"login: sdk.login() returned in {time.perf_counter() - t0:.1f}s")
        if not getattr(result, "is_success", False):
            raise RuntimeError(f"login failed: {getattr(result, 'message', result)}")

        _log_debug("login: calling sdk.init_realtime()…")
        t0 = time.perf_counter()
        sdk.init_realtime()
        _log_debug(f"login: init_realtime() returned in {time.perf_counter() - t0:.1f}s")
        ws = sdk.marketdata.websocket_client.stock
        conn = {"sdk": sdk, "ws": ws, "codes": set(), "sub_ids": {}, "state": "connecting", "last_error": None}
        # Before connect(): the socket starts emitting as soon as it authenticates.
        ws.on("message", lambda raw: _handle_message(conn, raw))
        ws.on("connect", lambda *a, **k: _on_connect(conn))
        ws.on("disconnect", lambda *a, **k: _on_disconnect(conn))
        ws.on("error", lambda *a, **k: _on_error(conn, a, k))
        _log_debug("login: calling ws.connect() (websocket handshake, no client-side timeout)…")
        t0 = time.perf_counter()
        ws.connect()
        _log_debug(f"login: ws.connect() returned in {time.perf_counter() - t0:.1f}s — connection ready")
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
    _log_debug(f"_ensure_connection: connection #{idx} doesn't exist yet — opening a fresh login "
               f"(this is the one un-timed network call in the whole subscribe path)")
    conn = _login_new_connection()
    with _lock:
        conn["index"] = len(_connections)
        _connections.append(conn)
    return conn


def _preopen_connections(target_total_subs):
    """Open however many pooled connections `target_total_subs` subscriptions
    will need, all at once, in parallel.

    Each login is a slow, untimed network round trip (broker auth + cert
    download + websocket handshake, ~7-8s measured against this account) and
    `_ensure_connection` only ever opens the next missing one just-in-time,
    one at a time, as `_subscribe_one`'s otherwise-fast serial loop first
    runs out of room on the last connection. A cold start big enough to need
    N connections (a 1,000-code chain needs 4) used to pay N serial logins
    back to back — ~24-30s of pure network wait — before the later
    connections could even start subscribing. Opening every connection this
    batch will need up front, concurrently, turns that into about one
    login's worth of wall time.

    Best-effort: a failed parallel open here just means `_subscribe_one`'s
    normal lazy path opens it (again, alone) when it actually needs that
    slot — same fallback behavior as if this function didn't run at all.
    """
    with _lock:
        have = len(_connections)
    needed = live_warrant_logic.connections_needed(target_total_subs) - have
    if needed <= 0:
        return
    _log_debug(f"_preopen_connections: opening {needed} connection(s) in parallel "
               f"(target {have + needed} for {target_total_subs} subscriptions)")
    with ThreadPoolExecutor(max_workers=needed) as pool:
        futures = [pool.submit(_ensure_connection, have + i) for i in range(needed)]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                print(f"LIVEWARRANT: parallel connection open failed: {_error_detail(e)}", flush=True)


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
        # Tick-log capture (services/live_tick_log.py) for the Live Arb tab's
        # Download CSV button — TSMC only, gated behind the recorder's own
        # on/off flag so this costs one bool check per tick when it's off.
        record_tick = live_tick_log.is_active() and _underlying_of.get(code) == "2330"
        if record_tick:
            terms = _terms.get(code) or {}
            tick_name = _display_name(code)
            tick_best = live_warrant_logic.best_level(new_bids, new_asks)

    if record_tick:
        maturity = terms.get("maturity")
        live_tick_log.record({
            "ts": datetime.now(live_tick_log.TW_TZ).isoformat(timespec="milliseconds"),
            "kind": "warrant",
            "code": code,
            "name": tick_name,
            "type": live_warrant_logic.parse_warrant_type(tick_name),
            "strike": terms.get("strike"),
            "expiry": maturity.isoformat() if maturity else None,
            "dte": _days_to_expiry(maturity),
            "bid": tick_best.get("bid"),
            "ask": tick_best.get("ask"),
            "bid_size": tick_best.get("bid_size"),
            "ask_size": tick_best.get("ask_size"),
            "src": "ws",
        })


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
        t0 = time.perf_counter()
        try:
            q = _rest_call(f"quote {code}",
                           sdk.marketdata.rest_client.stock.intraday.quote, symbol=code)
            elapsed = time.perf_counter() - t0
            if elapsed > SLOW_CALL_LOG_THRESHOLD_S:
                _log_debug(f"seed {code}: REST quote call took {elapsed:.1f}s "
                           f"(attempt {attempt}/{SEED_RETRIES}) — slower than expected")
            break
        except Exception as e:
            elapsed = time.perf_counter() - t0
            detail = _error_detail(e)
            if elapsed > SLOW_CALL_LOG_THRESHOLD_S:
                _log_debug(f"seed {code}: REST quote call failed after {elapsed:.1f}s "
                           f"(attempt {attempt}/{SEED_RETRIES}): {detail}")
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
    this is a separate round trip. Cached permanently on success (and
    persisted to Supabase — see `db_live_warrant.update_terms` — so a future
    restart can load it back instead of spending another REST round trip); a
    failure is left uncached so the next retry_pending() pass picks it up
    again, and is deliberately NOT written to `_seed_errors`, which tracks
    the streaming path.
    """
    t0 = time.perf_counter()
    try:
        t = _rest_call(f"ticker {code}",
                       sdk.marketdata.rest_client.stock.intraday.ticker, symbol=code) or {}
        elapsed = time.perf_counter() - t0
        if elapsed > SLOW_CALL_LOG_THRESHOLD_S:
            _log_debug(f"terms {code}: REST ticker call took {elapsed:.1f}s — slower than expected")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        detail = _error_detail(e)
        if elapsed > SLOW_CALL_LOG_THRESHOLD_S:
            _log_debug(f"terms {code}: REST ticker call failed after {elapsed:.1f}s: {detail}")
        print(f"LIVEWARRANT: terms {code} failed: {detail}", flush=True)
        return False

    def num(key):
        try:
            v = float(t.get(key))
        except (TypeError, ValueError):
            return None
        return v or None  # 0 means "not applicable" in this payload, not a real 0

    with _lock:
        terms = _terms[code] = {
            "strike": num("exercisePrice"),
            "exercise_ratio": num("exerciseRatio"),
            "maturity": _parse_maturity(t.get("maturityDate")),
        }
        if t.get("name"):
            _names.setdefault(code, t["name"])
    try:
        db_live_warrant.update_terms(code, terms["strike"], terms["exercise_ratio"], terms["maturity"])
    except Exception as e:
        print(f"LIVEWARRANT: terms persist {code} failed: {e}", flush=True)
    return True


def _reseed_one(code):
    """One reseed attempt for retry_pending()'s parallel fan-out (see its
    docstring for why this runs on a thread pool instead of one code at a
    time). Same REST call + name-backfill retry_pending() used to do inline."""
    with _lock:
        conn = next((c for c in _connections if code in c["codes"]), None)
    if not (conn and _seed_from_rest(conn["sdk"], code)):
        return False
    with _lock:
        name = _names.get(code)
    if name:
        try:
            db_live_warrant.update_name(code, name)
        except Exception as e:
            print(f"LIVEWARRANT: name backfill {code} failed: {e}", flush=True)
    return True


def _fetch_terms_one(code):
    """One terms fetch for retry_pending()'s parallel fan-out."""
    with _lock:
        conn = _connections[0] if _connections else None
    return bool(conn and _fetch_terms(conn["sdk"], code))


def _fan_out_start(fn, codes, deadline):
    """Submit `fn(code)` for each code across SEED_WORKERS threads and return
    immediately, WITHOUT waiting on any of them — a `ThreadPoolExecutor`
    starts running submitted work right away, before the caller ever calls
    `_fan_out_finish`. Splitting submit from wait is what lets
    `retry_pending()` launch several REST-bound batches (reseed, terms)
    against the same shared REST quota AT THE SAME TIME instead of running
    one to completion before the next even starts — seen directly in
    production: a reseed batch that got stuck on quota contention once ate
    53 of a 60s budget, leaving the terms batch only 6.6s to work with, even
    though both batches draw from the exact same underlying quota anyway and
    have nothing to gain from being serialized.
    """
    if not codes:
        return None, []
    label = getattr(fn, "__name__", "fn")
    remaining = max(0.0, deadline - time.monotonic())
    _log_debug(f"_fan_out({label}): starting {len(codes)} codes on {SEED_WORKERS} workers, "
               f"{remaining:.1f}s left in budget")
    pool = ThreadPoolExecutor(max_workers=SEED_WORKERS)
    futures = [pool.submit(fn, code) for code in codes]
    return pool, futures


def _fan_out_finish(fn, pool, futures, deadline):
    """Wait (up to `deadline`, a `time.monotonic()` timestamp) for a batch
    `_fan_out_start` already launched, then tear down its pool.

    Never blocks the caller past `deadline` — past it, whatever hasn't
    finished is simply not waited on; its thread keeps running in the
    background (still bounded by the same quota/retry ceiling) and updates
    state under `_lock` whenever it completes, it just no longer holds this
    HTTP response open. Left-over codes are picked up again by the next
    retry_pending() call exactly as if they'd failed outright.
    """
    if pool is None:
        return 0
    label = getattr(fn, "__name__", "fn")
    try:
        remaining = max(0.0, deadline - time.monotonic())
        done, not_done = futures_wait(futures, timeout=remaining)
        ok = sum(1 for f in done if not f.exception() and f.result())
        if not_done:
            _log_debug(f"_fan_out({label}): budget ran out — {len(done)}/{len(futures)} finished "
                       f"({ok} succeeded), {len(not_done)} still running in the background")
        else:
            _log_debug(f"_fan_out({label}): all {len(futures)} finished, {ok} succeeded")
        return ok
    finally:
        # wait=False: don't block here either — the still-running futures
        # finish (or give up) on their own; a Python ThreadPoolExecutor's
        # worker threads aren't forcibly killable anyway.
        pool.shutdown(wait=False)


def _fan_out(fn, codes, deadline):
    """Run `fn(code)` for each code across SEED_WORKERS threads, but never
    block the caller past `deadline` (a `time.monotonic()` timestamp).

    Parallelizing bounds how many codes run AT ONCE; it does not bound how
    LONG the call can take — a single quota-starved `fn` can itself take up
    to ~SEED_RETRIES*(REST_ACQUIRE_TIMEOUT_S+backoff) seconds, and waiting
    for every one of a large batch to finish (what `pool.map` does) could
    still exceed gunicorn's request timeout under a big enough backlog.

    Convenience wrapper around `_fan_out_start`/`_fan_out_finish` for a
    single independent batch; `retry_pending()` calls the split form
    directly so multiple batches can run concurrently against one shared
    deadline instead of one at a time through this wrapper.
    """
    pool, futures = _fan_out_start(fn, codes, deadline)
    return _fan_out_finish(fn, pool, futures, deadline)


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
    gunicorn's request timeout and leaves the scan half-applied. The
    subscribe loop itself is still serial (see `_subscribe_one`) — each send
    is a fast local operation — but every connection this batch will need is
    opened in parallel up front (`_preopen_connections`) so that loop is
    never stuck waiting through several logins back to back; only the REST
    seed fans out beyond that.

    A per-code subscribe failure is collected and returned, never raised: this
    used to abort the whole batch, which left the codes it had already reached
    subscribed-but-unpersisted and the codes it never reached missing outright.
    Anything that fails here stays in `_pending` and is picked up by
    retry_pending() on the next scheduler tick.
    """
    if codes:
        with _lock:
            current_subs = sum(len(c["codes"]) for c in _connections)
        _preopen_connections(current_subs + len(codes))
        _log_debug(f"_track_many: subscribing {len(codes)} codes (serial — each is a fast local "
                   f"send UNLESS a new connection needs to open, see _ensure_connection's own log)")
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
    if codes:
        _log_debug(f"_track_many: subscribe done, {len(conns)}/{len(codes)} subscribed — "
                   f"starting REST seed fan-out")
    # Seeding is quota-bound (REST_QUOTA/minute), so a whole-chain scan cannot
    # seed everything inside one web request. Spend the budget, leave the rest
    # unseeded — those codes are subscribed and will fill from the websocket on
    # the first requote, and retry_pending() seeds them at leisure.
    to_seed = conns if seed_budget is None else conns[:seed_budget]
    if to_seed:
        with ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
            list(pool.map(lambda pair: _seed_from_rest(pair[1]["sdk"], pair[0]), to_seed))
        _log_debug(f"_track_many: REST seed fan-out done for {len(to_seed)} codes")
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
    """Idempotent: no-op if the pool already has connections or the user
    disconnected; otherwise loads the persisted tracked list and resubscribes
    it.

    Subscribing is a local send with no round trip (see `_subscribe_one`), so
    every persisted code is subscribed up front even for a full ~1,050-code
    chain; only the REST seed is quota-bound, so it's spent the same way
    `scan_underlying`'s `_track_many` already budgets a fresh scan —
    `SEED_BUDGET_PER_SCAN` inline, the rest left for `retry_pending()` to
    drain over the next few ticks. Seeding one code at a time used to make a
    redeploy's restart minutes slower than a fresh scan for no reason; this
    is what makes a restart resubscribe exactly as fast as a scan does.
    """
    global _session_error
    with _lock:
        if _connections or _stopped_by_user:
            return
    _session_error = None
    _log_debug("start_session: loading persisted tracked list from Supabase…")
    _log_resource_snapshot("start_session start")

    try:
        rows = db_live_warrant.list_tracked()
    except Exception as e:
        _session_error = f"failed to load tracked list: {type(e).__name__}: {e}"
        print(f"LIVEWARRANT: {_session_error}", flush=True)
        return
    _log_debug(f"start_session: loaded {len(rows)} persisted rows")

    with _lock:
        _tracked.clear()
        _pending.clear()
        _seeded.clear()
        _seed_errors.clear()
        _underlying_of.clear()
        # `_terms` deliberately survives a restart of the session: a warrant's
        # strike, ratio and maturity never change, and re-fetching them would
        # spend a full quota window on data we already have. For a genuinely
        # fresh process (the common case — `_terms` is in-memory only) this
        # loop reloads it from `live_warrant_tracked` instead (migration 024)
        # for any code whose terms were already looked up before a previous
        # restart — `terms_fetched_at` set is what "already looked up" means;
        # the value columns alone can't tell that apart from "never looked
        # up" since a malformed payload can leave one of them null and still
        # count as done (see _fetch_terms).
        terms_loaded = 0
        for row in rows:
            code = row["code"]
            stored = row.get("name")
            if stored and not _is_placeholder_name(stored):
                _names[code] = stored
            _tracked.append(code)
            _pending.add(code)
            _underlying_of[code] = row.get("underlying")
            if code not in _terms and row.get("terms_fetched_at") is not None:
                maturity_raw = row.get("maturity")
                _terms[code] = {
                    "strike": row.get("strike"),
                    "exercise_ratio": row.get("exercise_ratio"),
                    "maturity": date.fromisoformat(maturity_raw) if maturity_raw else None,
                }
                terms_loaded += 1
    if terms_loaded:
        _log_debug(f"start_session: loaded {terms_loaded} codes' contract terms from Supabase "
                   f"— {len(rows) - terms_loaded} still need a REST fetch")

    # Underlying-price subscriptions are not persisted anywhere — re-derived
    # from the tracked list's own `underlying` column every (re)start, same as
    # the warrant subscriptions themselves. Subscribed BEFORE the warrant
    # chain below (same ordering scan_underlying already uses) so the
    # underlying's own display row starts ticking immediately instead of
    # waiting behind however many hundred warrant subscribes come after it.
    with _lock:
        underlyings = {u for u in _underlying_of.values() if u in UNDERLYING_LIVE_PRICE_ALLOWLIST}
    if underlyings:
        _log_debug(f"start_session: subscribing underlyings {sorted(underlyings)} before the warrant chain")
    for underlying in underlyings:
        _subscribe_underlying(underlying)

    codes = [row["code"] for row in rows]
    _log_debug(f"start_session: subscribing/seeding {len(codes)} warrant codes "
               f"(seed budget {SEED_BUDGET_PER_SCAN})")
    failed = _track_many(codes, seed_budget=SEED_BUDGET_PER_SCAN)
    _log_debug(f"start_session: _track_many done, {len(codes) - len(failed)}/{len(codes)} subscribed")
    _log_resource_snapshot("start_session end")
    if failed:
        _session_error = (f"{failed[0]}: subscribe failed"
                          + (f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""))

    # The seed above may have produced the first real name for a row written
    # before it was subscribed, or for one still carrying an old
    # "(FugleAPIError)" placeholder. Write it back so the pollution clears on
    # its own rather than being re-read every restart. Only codes actually
    # seeded within this call's budget need it here — a code left for
    # retry_pending() to seed later gets its own name backfill from that
    # path's identical check once it catches up.
    for row in rows:
        code, stored = row["code"], row.get("name")
        with _lock:
            seeded, fresh = code in _seeded, _names.get(code)
        if seeded and fresh and fresh != stored:
            try:
                db_live_warrant.update_name(code, fresh)
            except Exception as db_e:
                print(f"LIVEWARRANT: name backfill {code} failed: {db_e}", flush=True)


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
    and never change, so the work runs down to nothing.

    Every REST-bound step (subscribe, reseed, terms) fans out over
    SEED_WORKERS threads via `_fan_out`, all sharing ONE
    RETRY_PENDING_BUDGET_S deadline — see `_fan_out`'s docstring for why
    parallelism alone (bounding how many codes run at once) isn't enough:
    `pool.map()` still blocks until every submitted call finishes, and one
    quota-starved call can itself take up to ~SEED_RETRIES*
    (REST_ACQUIRE_TIMEOUT_S+backoff) seconds. A big backlog (e.g. right
    after a redeploy) could still make an unbounded parallel version exceed
    gunicorn's request timeout and get killed mid-batch — the exact "click
    Retry Pending, it fills a few rows then stops" symptom. Whatever doesn't
    finish inside the shared budget is simply left for the next call.

    The reseed and terms fan-outs are launched together (`_fan_out_start`
    for both before either is awaited) rather than one after the other:
    they draw from the exact same REST quota regardless, so serializing them
    only starves whichever runs second whenever the first is quota-starved
    — observed directly in production, where a slow reseed stage ate 53 of
    a 60s budget and left the terms stage only 6.6s.

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

    _log_debug(f"retry_pending: start — {len(todo)} to subscribe, {len(reseed)} to reseed, "
               f"{len(need_terms)} needing terms, budget {RETRY_PENDING_BUDGET_S}s")
    _log_resource_snapshot("retry_pending start")
    deadline = time.monotonic() + RETRY_PENDING_BUDGET_S

    if todo:
        with _lock:
            current_subs = sum(len(c["codes"]) for c in _connections)
        _preopen_connections(current_subs + len(todo))

    # Subscribing is a cheap local send with no round trip ONLY when an
    # already-open connection has room (_subscribe_one -> _ensure_connection
    # returns immediately in that case). When every existing connection is
    # full, _ensure_connection instead calls _login_new_connection() — a
    # real, untimed network round trip (broker login + cert download +
    # websocket handshake) that this loop previously ran for every such code
    # with NO deadline check at all, silently bypassing the whole budget
    # below it. A slow or stuck login here could burn the entire time budget
    # (or more) before _fan_out ever got a chance to run — which looks
    # exactly like "click Retry Pending, it fills a couple rows then stops
    # entirely," just from a different cause than the REST-quota one this
    # budget was originally built for. Checking the deadline here too closes
    # that gap: once time's up, whatever's left in `todo` just stays pending
    # for the next call, same as always.
    to_seed = []
    for i, code in enumerate(todo):
        if time.monotonic() >= deadline:
            _log_debug(f"retry_pending: subscribe stage hit its deadline after "
                       f"{i}/{len(todo)} codes — {len(todo) - i} left pending for next call")
            break
        try:
            _subscribe_one(code)
            with _lock:
                _pending.discard(code)
            to_seed.append(code)
        except Exception as e:
            detail = _error_detail(e)
            with _lock:
                _pending.add(code)
                _seed_errors[code] = f"subscribe failed: {detail}"
            print(f"LIVEWARRANT: retry subscribe {code} failed: {detail}", flush=True)
    subscribed = len(to_seed)
    _log_debug(f"retry_pending: subscribe stage done, {subscribed}/{len(todo)} subscribed — "
               f"starting seed+terms fan-out")

    # Launch every REST-bound batch's pool BEFORE awaiting any of them: a
    # ThreadPoolExecutor's workers start pulling submitted work immediately,
    # so by the time we get around to awaiting the first batch, the other
    # two are already running concurrently against it — none of them can
    # starve another out of the shared deadline just by being slow.
    to_seed_pool, to_seed_futures = _fan_out_start(_reseed_one, to_seed, deadline)
    reseed_pool, reseed_futures = _fan_out_start(_reseed_one, reseed, deadline)
    terms_pool, terms_futures = _fan_out_start(_fetch_terms_one, need_terms, deadline)

    _fan_out_finish(_reseed_one, to_seed_pool, to_seed_futures, deadline)
    reseeded = _fan_out_finish(_reseed_one, reseed_pool, reseed_futures, deadline)
    termed = _fan_out_finish(_fetch_terms_one, terms_pool, terms_futures, deadline)

    with _lock:
        still = len(_pending)
        missing_terms = sum(1 for c in _tracked if c not in _terms)
    _log_debug(f"retry_pending: done — subscribed={subscribed} reseeded={reseeded} "
               f"terms={termed} pending={still} terms_missing={missing_terms}")
    _log_resource_snapshot("retry_pending end")
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
        # _underlying_codes is also a "currently subscribed" set, same as
        # conn["codes"] — but unlike the tracked warrants, nothing re-derives
        # it from _pending. Left uncleared, _subscribe_underlying's dedup
        # guard ("if code in _underlying_codes: return") sees the stale entry
        # on the next start_session() and skips resubscribing it for good,
        # even though the connection that held it is gone: the underlying's
        # row freezes on its last price while every warrant recovers normally.
        # _underlying_books goes with it so the row reads "pending" again
        # instead of silently keeping the stale price.
        _underlying_codes.clear()
        _underlying_books.clear()
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


def remove_underlying(underlying):
    """Drop every tracked warrant for one underlying in a single action —
    the bulk counterpart to remove_code(), for clearing a whole chain you
    scanned and no longer want (e.g. "remove Hon Hai") without clicking
    Remove on every one of its warrants individually.

    Only ever matches scan-sourced rows: `underlying` is non-null exclusively
    for `source='scan'` rows (see the DB check constraint in migration 023),
    so a manually-added code is never swept up by this. Does not touch the
    underlying's own live-price subscription (UNDERLYING_LIVE_PRICE_ALLOWLIST)
    — that stays subscribed for whichever other tracked warrants still
    reference it, and simply goes unused if this was the last one.

    Returns the number of codes removed.
    """
    with _lock:
        codes = [c for c, u in _underlying_of.items() if u == underlying]
    _remove_many(codes)
    return len(codes)


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


def snapshot_for_underlying(underlying):
    """Read-only cross-module accessor for Live Arb (services/live_arb.py):
    static terms + best bid/ask/size for every tracked warrant on
    `underlying`, plus a tick-seq sum that only changes when one of those
    warrants' best level actually moves — the dirty-gate signal live_arb.py
    polls on a fast timer instead of diffing raw books itself, reusing the
    same `_book_seq` this module already maintains for its own tick-driven
    recompute (see `_handle_message`/`best_level_changed`).
    """
    with _lock:
        codes = [c for c in _tracked if _underlying_of.get(c) == underlying]
        seq = sum(_book_seq.get(c, 0) for c in codes)
        rows = []
        for code in codes:
            book = _books.get(code)
            terms = _terms.get(code) or {}
            name = _display_name(code)
            best = (live_warrant_logic.best_level(book["bids"], book["asks"])
                    if book else live_warrant_logic.best_level([], []))
            rows.append({
                "code": code, "name": name,
                "type": live_warrant_logic.parse_warrant_type(name),
                "strike": terms.get("strike"),
                "exercise_ratio": terms.get("exercise_ratio"),
                "maturity": terms.get("maturity"),
                "best": best,
                # Raw book metadata, unrelated to the arb-matching fields
                # above — feeds live_arb_logic.latest_tick()'s "Last
                # received tick" debug line, not the matcher itself.
                "ts": book["ts"] if book else None,
                "src": book.get("src") if book else None,
            })
    return seq, rows


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
            "strike": None, "exercise_ratio": None, "dte": None,
            "time_value": None, "bid_time_value_pct": None, "ask_time_value_pct": None,
            "best": live_warrant_logic.best_level([], []),
            "age": None, "src": None,
        }
    return {
        "code": code, "name": name, "pending": False, "error": None,
        "type": "Underlying", "underlying_code": code, "underlying_price": info.get("price"),
        "strike": None, "exercise_ratio": None, "dte": None,
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
    strike/DTE.
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

    best = live_warrant_logic.best_level(bids, asks)
    bid, ask = best.get("bid") or 0.0, best.get("ask") or 0.0
    is_put = type_label == "Put"

    time_value, bid_pct, ask_pct = iv_engine.solve_tick(
        underlying_price, strike, ratio, is_put, bid, ask)

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


def _log_debug(message):
    """A diagnostic checkpoint, surfaced in the Live Warrant tab's console
    log (the only kind of entry it carries) so a freeze can be diagnosed by
    watching the browser instead of guessed at from code inspection — the
    last checkpoint logged before everything stops is, by construction,
    right next to whatever's actually stuck.

    Deliberately cheap (one dict append under the same lock every other
    console-log write already uses) and left on permanently rather than
    behind a flag: the whole point is to have this on the FIRST time a
    freeze happens on the real site, not the first time after someone
    remembers to turn on debug logging.
    """
    global _console_log_next_id
    with _lock:
        _console_log_next_id += 1
        _console_log.append({
            "id": _console_log_next_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "code": "-",
            "diff": message,
            "recalculated": "",
            "duration_s": None,
            "level": "debug",
        })
    print(f"LIVEWARRANT DEBUG: {message}", flush=True)


# Previous cpu.stat throttle reading, so each snapshot can report what's
# NEW since the last one rather than a lifetime-of-the-container total.
_last_cpu_throttle = {"nr": None, "usec": None}


def _log_resource_snapshot(label):
    """Memory/CPU-limit checkpoint, logged the same way as _log_debug (so
    it's yellow-highlighted in the console log too) — this is what lets a
    freeze be correlated with actual resource pressure instead of guessed
    at. Render's OOM killer sends the process an unblockable SIGKILL, so a
    real OOM has no "as it happens" log by construction; this is the closest
    available substitute — the trend leading up to a freeze, from whichever
    checkpoint ran last before everything stopped responding.

    CPU is reported as throttle events, not %: a busy-but-not-throttled
    process and one that's hitting its quota and getting paused by the
    kernel can both show high CPU%, but only the second one is actually
    "hitting the limit" — cgroup v2's cpu.stat counters are the one signal
    that distinguishes them.
    """
    frac = memlog.memory_usage_fraction()
    quota = memlog.cpu_quota_vcpus()
    nr, usec = memlog.cpu_throttle_stats()

    parts = []
    if frac is None:
        parts.append("memory limit unavailable (no cgroup — not running in a container?)")
    else:
        pct = round(frac * 100, 1)
        if pct >= 90:
            parts.append(f"memory {pct}% of container limit — CRITICAL, an OOM kill may be imminent")
        elif pct >= 75:
            parts.append(f"memory {pct}% of container limit — elevated")
        else:
            parts.append(f"memory {pct}% of container limit")

    if nr is None:
        parts.append("CPU throttle stats unavailable")
    else:
        with _lock:
            prev_nr, prev_usec = _last_cpu_throttle["nr"], _last_cpu_throttle["usec"]
            _last_cpu_throttle["nr"], _last_cpu_throttle["usec"] = nr, usec
        new_throttles = nr - prev_nr if prev_nr is not None else 0
        if new_throttles > 0:
            added_usec = (usec - prev_usec) if prev_usec is not None else usec
            parts.append(f"CPU THROTTLED {new_throttles} more time(s) since last check "
                        f"({added_usec / 1000:.0f}ms paused) — quota is {quota or '?'} vCPU")
        else:
            parts.append(f"CPU: no new throttling (quota {quota or 'unlimited'} vCPU)")

    _log_debug(f"{label}: " + "; ".join(parts))


def get_console_log(since=0, limit=200):
    """Debug-checkpoint log entries with id > `since`, oldest first, capped at `limit`."""
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
    base = {
        "code": code, "name": name, "pending": pending, "error": error,
        "type": live_warrant_logic.parse_warrant_type(name),
        "underlying_code": underlying,
        "underlying_price": underlying_price,
        "strike": terms.get("strike"),
        "exercise_ratio": terms.get("exercise_ratio"),
        "dte": _days_to_expiry(terms.get("maturity")),
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
