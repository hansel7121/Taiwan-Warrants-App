"""Live Options tab session: one Fubon account, a lazily-grown pool of
websocket connections against `sdk.marketdata.websocket_client.futopt` (not
`.stock` — TAIFEX single-stock options are a genuinely different Fubon
websocket client from warrants, see docs/fubon_connection_runbook.md), and
the in-process order-book cache the /live_options_data route reads from.

Structurally parallel to services/live_warrant.py — same connection-pool
shape, same subscribe/unsubscribe mechanics, same session lifecycle, and (as
of the REST-seed addition below) the same REST-seed-before-first-tick step —
but deliberately smaller: NO database persistence (the tracked contract list
is in-memory only, lost on process restart — re-click "Load TSMC Chain"), and
NO pricing computation of any kind (no IV, no Greeks, no time-value — this
tab exists purely to prove out the Fubon subscription mechanism for options,
one step before any analytics get layered on).

REST seed (`_seed_from_rest`): the books channel is update-only — it pushes
on change and sends no snapshot on subscribe — so a contract with no recent
activity would show "—" until a market maker requotes it. Every newly
subscribed contract gets one REST quote to fill its book immediately,
exactly mirroring live_warrant.py's identical-purpose helper, with one
option-specific wrinkle: futopt's quote() carries no bids/asks ladder at all
(unlike stock's) — only a single lastTrade.bid/ask — confirmed live against
a 2330 chain by scripts/fubon_quote_viewer.py's own `_seed_from_rest`, which
this mirrors. A REST-seeded book is marked `src: "rest"` so the UI can show
it as a snapshot (age included) until the first live tick overwrites it with
`src: "ws"`. This is also what makes Live Arb (services/live_arb.py), which
just reads this module's tracked books, see a contract's price the instant
it's tracked rather than only after its first requote.

TSMC (2330) only for now — SUPPORTED_UNDERLYING mirrors the exact framing of
live_warrant.py's UNDERLYING_LIVE_PRICE_ALLOWLIST.

HIGH RISK, read before trusting this in production: what Fugle's
`tickers(type="OPTION", ...)` response actually carries for strike price and
call/put is UNVERIFIED against a live account — see
logic/live_options_logic.py's module docstring and `load_chain`'s one-shot
diagnostic log below.

Connection assignment and the subscription cap are delegated to
logic/live_warrant_logic.py (reused directly — those functions are already
generic, no warrant-specific assumptions); contract-field parsing and the
add-only chain diff are delegated to logic/live_options_logic.py. This
module is the impure glue around them and, like live_warrant.py, is
verified manually against the real Fubon connection during trading hours,
not by the test suite (except for the one targeted _handle_message test in
tests/services/test_live_options_ticks.py).
"""
import collections
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from logic import live_options_logic, live_warrant_logic
from services import tick_recorder
from services.broker import credentials as broker_credentials

FUBON_CRED_LABEL = os.environ.get("FUBON_CRED_LABEL", broker_credentials.DEFAULT_LABEL)
BOOKS_CHANNEL = "books"

RECONNECT_RETRIES = 3
RECONNECT_BACKOFF_S = 5
# Codes re-attempted per retry_pending() call. Bounded so a scheduler tick
# that inherits a fully failed chain load still finishes promptly.
PENDING_RETRY_BATCH = 100
# Parallel REST seed workers during a chain load — same figure as
# live_warrant.py's SEED_WORKERS, same reasoning (bounded well under the
# per-second quota the Fugle REST endpoint enforces).
SEED_WORKERS = 8
SEED_RETRIES = 3
SEED_BACKOFF_S = 0.6
# REST seeds a single load_chain() request will spend inline before leaving
# the rest for retry_pending() — a full TSMC option chain (many strikes ×
# several expiries × 2 types) can run past what fits in one request the same
# way a full warrant chain does, see live_warrant.py's SEED_BUDGET_PER_SCAN.
SEED_BUDGET_PER_LOAD = 200
# Same measured Fugle account-wide REST ceiling live_warrant.py's _RestQuota
# guards against — duplicated here rather than imported (see this module's
# reuse-vs-duplicate note in the docstring above and live_options.js's
# identical note): this module's futopt session is a wholly separate SDK
# login from live_warrant.py's stock session, so there is nothing genuinely
# shared to import.
REST_QUOTA = 300
REST_QUOTA_WINDOW_S = 60.0
REST_BURST_LIMIT = 24
REST_BURST_WINDOW_S = 1.0
REST_ACQUIRE_TIMEOUT_S = 20.0

# v1 hard-coded to one underlying, same framing as live_warrant.py's
# UNDERLYING_LIVE_PRICE_ALLOWLIST — trivially widened once the contract-field
# parsing above is confirmed correct against a live account.
SUPPORTED_UNDERLYING = "2330"

_lock = threading.RLock()
_books = {}        # code -> {"bids": [...], "asks": [...], "ts": datetime, "src": "ws"|"rest"}
_contracts = {}     # code -> {"expiry": date, "strike": float, "is_put": bool, "name": str}
_tracked = []       # codes, add order. The ONLY persistence there is — lost on process restart.
_connections = []   # each: {"sdk", "ws", "codes": set(), "sub_ids": {}, "state", "last_error"}
_pending = set()    # tracked codes with no live subscription — retried by retry_pending()
_seeded = set()     # codes whose REST seed completed (an empty book still counts, see _seed_from_rest)
_track_errors = {}  # code -> last subscribe/seed error, cleared on success
_session_error = None  # set when the session can't even start (e.g. no credentials)
_stopped_by_user = False  # set by stop_session(); blocks scheduler/load_chain from silently reopening it
_diagnostic_logged = False  # one-shot: log the first raw tickers() row so the field-shape guess can be checked
# Bumped on every books tick. Nothing in this module needs per-code dirty
# tracking (see the module docstring — no derived columns here), but Live
# Arb (services/live_arb.py) needs SOME cheap "has anything changed since I
# last scanned" signal, mirroring live_warrant.py's per-code `_book_seq`
# without needing the per-code granularity that module's tick-driven
# recompute actually requires.
_tick_seq = 0


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics helpers
# ─────────────────────────────────────────────────────────────────────────────
def _error_detail(e):
    """One readable line for an SDK error — see live_warrant.py's identical
    helper for why FugleAPIError needs this instead of a bare str()."""
    status = getattr(e, "status_code", None)
    message = getattr(e, "message", None)
    if status or message:
        return " ".join(str(x) for x in (type(e).__name__, status, message) if x)
    return f"{type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# REST quota — identical mechanism to live_warrant.py's `_RestQuota` (see its
# docstring for the full sliding-window + burst-window reasoning); duplicated
# rather than shared since this module's futopt SDK session is a separate
# login from that module's stock session, see this module's docstring.
# ─────────────────────────────────────────────────────────────────────────────
class _RestQuota:
    def __init__(self, limit=REST_QUOTA, window=REST_QUOTA_WINDOW_S,
                 burst_limit=REST_BURST_LIMIT, burst_window=REST_BURST_WINDOW_S):
        self._limit = limit
        self._window = window
        self._burst_limit = burst_limit
        self._burst_window = burst_window
        self._hits = collections.deque()
        self._lock = threading.Lock()

    def _burst_count_and_oldest(self, now):
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
            time.sleep(min(wait, 0.25) + 0.01)


_rest_quota = _RestQuota()


def _rest_call(label, fn, **params):
    """Run one REST call against the shared quota."""
    if not _rest_quota.acquire(timeout=REST_ACQUIRE_TIMEOUT_S):
        raise RuntimeError(
            f"REST quota exhausted: no slot for {label} within {REST_ACQUIRE_TIMEOUT_S:.0f}s "
            f"({REST_QUOTA}/{REST_QUOTA_WINDOW_S:.0f}s)")
    return fn(**params)


# ─────────────────────────────────────────────────────────────────────────────
# Login / connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def _login_new_connection():
    """Log in fresh and open one websocket connection against the futopt
    client. Raises on any failure. Always a FRESH FubonSDK().login() — never
    reuses a warrant connection's sdk instance (see the module docstring's
    login-cap note: the account-wide login cap is shared across stock+futopt,
    so this module keeps its own pool rather than assuming a session can be
    shared, which is unverified and untested).

    Callbacks close over the `conn` dict directly, not a list index — see
    live_warrant.py's identical `_login_new_connection` for why.
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
        ws = sdk.marketdata.websocket_client.futopt
        conn = {"sdk": sdk, "ws": ws, "codes": set(), "sub_ids": {}, "state": "connecting", "last_error": None}
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
    """Fold one books message into the shared cache. No dirty-tracking —
    unlike live_warrant.py there's nothing downstream to recompute, so
    there's nothing to gate."""
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

    tick_recorder.record("option", code, data.get("bids") or [], data.get("asks") or [])

    global _tick_seq
    with _lock:
        _books[code] = {
            "bids": data.get("bids") or [],
            "asks": data.get("asks") or [],
            "ts": datetime.now(timezone.utc),
            "src": "ws",
        }
        _tick_seq += 1


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
            print(f"LIVEOPTIONS: conn {conn.get('index', '?')} error {conn['last_error']}", flush=True)


def _on_connect(conn):
    with _lock:
        conn["state"] = "connected"
    print(f"LIVEOPTIONS: conn {conn.get('index', '?')} connected", flush=True)


def _on_disconnect(conn):
    with _lock:
        was_connected = conn["state"] == "connected"
        conn["state"] = "disconnected"
    print(f"LIVEOPTIONS: conn {conn.get('index', '?')} disconnected", flush=True)
    if was_connected:
        threading.Thread(target=_reconnect_connection, args=(conn,), daemon=True).start()


def _on_error(conn, args, kwargs):
    with _lock:
        conn["state"] = "error"
        conn["last_error"] = "; ".join(str(x) for x in args) or repr(kwargs)
    print(f"LIVEOPTIONS: conn {conn.get('index', '?')} error {args!r} {kwargs!r}", flush=True)


def _reconnect_connection(conn):
    """A single connection's own auto-heal: re-login and resubscribe its own
    codes. Identical shape to live_warrant.py's version."""
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
            print(f"LIVEOPTIONS: conn {idx} reconnected, resubscribed {len(codes)} codes", flush=True)
            return
        except Exception as e:
            print(f"LIVEOPTIONS: conn {idx} reconnect attempt {attempt} failed: {e}", flush=True)
            time.sleep(RECONNECT_BACKOFF_S * attempt)

    with _lock:
        conn["state"] = "error"
        conn["last_error"] = "auto-reconnect exhausted retries"
    print(f"LIVEOPTIONS: conn {idx} auto-reconnect exhausted retries", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Per-code subscribe/unsubscribe
# ─────────────────────────────────────────────────────────────────────────────
def _subscribe_one(code):
    """Place one code on whichever pool connection has room, opening one if
    needed. Identical shape to live_warrant.py's version — reuses
    live_warrant_logic.assign_slot, same first-fit, same MAX_SUBS_PER_CONN/
    MAX_CONNECTIONS ceiling."""
    with _lock:
        counts = [len(c["codes"]) for c in _connections]
    idx = live_warrant_logic.assign_slot(counts)
    conn = _ensure_connection(idx)
    conn["ws"].subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
    with _lock:
        conn["codes"].add(code)
    return conn


def _seed_from_rest(sdk, code):
    """One REST quote per newly subscribed code: fills its first book before
    any websocket tick arrives, mirroring live_warrant.py's identical-purpose
    helper (see this module's docstring for the futopt-specific wrinkle).

    Failure is retried, then recorded in `_track_errors` and left to
    retry_pending() — never raises. `_seeded` is set on any successful REST
    round trip, including one whose book comes back empty (a contract can
    genuinely have no resting quote on either side) — that distinction is
    what stops an empty book from being re-seeded on every retry_pending()
    pass forever. Returns True when the quote came back.
    """
    for attempt in range(1, SEED_RETRIES + 1):
        try:
            q = _rest_call(f"quote {code}", sdk.marketdata.rest_client.futopt.intraday.quote, symbol=code)
            break
        except Exception as e:
            detail = _error_detail(e)
            if attempt == SEED_RETRIES:
                with _lock:
                    _track_errors[code] = f"seed failed: {detail}"
                print(f"LIVEOPTIONS: seed {code} failed after {attempt} attempts: {detail}", flush=True)
                return False
            time.sleep(SEED_BACKOFF_S * attempt)

    with _lock:
        _track_errors.pop(code, None)
        _seeded.add(code)

    # futopt's quote() carries no bids/asks ladder (unlike stock's) — only a
    # single lastTrade.bid/ask — so this synthesizes a 1-level book from it,
    # exactly as scripts/fubon_quote_viewer.py's confirmed-live equivalent
    # does. The live books push (5 levels) overwrites it on the first requote.
    last = q.get("lastTrade") or {}
    size = last.get("size") or 0
    bids = [{"price": last["bid"], "size": size}] if last.get("bid") else []
    asks = [{"price": last["ask"], "size": size}] if last.get("ask") else []
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


def _reseed_one(code):
    """One reseed attempt for retry_pending()'s parallel fan-out — same
    purpose as live_warrant.py's identical helper."""
    with _lock:
        conn = next((c for c in _connections if code in c["codes"]), None)
    return bool(conn and _seed_from_rest(conn["sdk"], code))


def _track_one(code):
    """Subscribe one code and seed its first book. Raises if the subscribe
    itself fails (the caller decides what that means); a failed REST seed is
    recorded, not raised."""
    conn = _subscribe_one(code)
    with _lock:
        _pending.discard(code)
    _seed_from_rest(conn["sdk"], code)


def _track_many(codes, seed_budget=None):
    """Subscribe a batch, then seed the books in parallel — same shape as
    live_warrant.py's `_track_many` (see its docstring for why the REST seed
    fans out over a thread pool while subscribing itself stays serial)."""
    conns, failed = [], []
    for code in codes:
        try:
            conns.append((code, _subscribe_one(code)))
            with _lock:
                _pending.discard(code)
        except Exception as e:
            detail = _error_detail(e)
            failed.append(code)
            with _lock:
                _pending.add(code)
                _track_errors[code] = f"subscribe failed: {detail}"
            print(f"LIVEOPTIONS: subscribe {code} failed: {detail}", flush=True)

    to_seed = conns if seed_budget is None else conns[:seed_budget]
    if to_seed:
        with ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
            list(pool.map(lambda pair: _seed_from_rest(pair[1]["sdk"], pair[0]), to_seed))
    if len(to_seed) < len(conns):
        print(f"LIVEOPTIONS: seeded {len(to_seed)}/{len(conns)} inline, "
              f"{len(conns) - len(to_seed)} left for retry_pending", flush=True)
    return failed


def _untrack_one(code):
    """Unsubscribe one code from whichever connection holds it. Diagnostics
    state is cleared first — see live_warrant.py's identical ordering note."""
    with _lock:
        _pending.discard(code)
        _seeded.discard(code)
        _track_errors.pop(code, None)
        _contracts.pop(code, None)
        conn = next((c for c in _connections if code in c["codes"]), None)
        sub_id = conn["sub_ids"].get(code) if conn else None
    if conn is None:
        return
    try:
        if sub_id:
            conn["ws"].unsubscribe({"id": sub_id})
    except Exception as e:
        print(f"LIVEOPTIONS: unsubscribe {code} failed: {e}", flush=True)
    with _lock:
        conn["codes"].discard(code)
        conn["sub_ids"].pop(code, None)
        _books.pop(code, None)


# ─────────────────────────────────────────────────────────────────────────────
# Session lifecycle (called by the scheduler gate and the manual Reconnect route)
# ─────────────────────────────────────────────────────────────────────────────
def start_session():
    """Idempotent no-op if: already connected, user disconnected, OR nothing
    has ever been loaded this process's lifetime (`_tracked` empty — unlike
    live_warrant.py there is no database to cold-load from, so a fresh
    process genuinely has nothing to resubscribe until `load_chain()` has
    been called at least once).

    Once `_tracked` is non-empty, this resubscribes every tracked code —
    including after a same-process teardown/reconnect, since `_teardown()`
    re-queues `_tracked` into `_pending` without ever clearing `_tracked`
    itself, exactly like live_warrant.py's version. This is what makes the
    scheduler tick (services/scheduler.py::sync_live_options) safe to run
    unconditionally: before any chain load it's a true no-op, and after one
    it behaves exactly like the warrant version.

    Uses `_track_many` (subscribe serially, then REST-seed in parallel,
    inline budget SEED_BUDGET_PER_LOAD) rather than looping `_track_one`
    per code — a reconnect on a large chain would otherwise pay one
    serial REST round trip per code back to back, exactly the slow path
    live_warrant.py's own start_session avoids the same way.
    """
    global _session_error
    with _lock:
        if _connections or _stopped_by_user or not _tracked:
            return
        codes = list(_tracked)
    _session_error = None
    failed = _track_many(codes, seed_budget=SEED_BUDGET_PER_LOAD)
    if failed:
        _session_error = (f"{failed[0]}: subscribe failed"
                          + (f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""))


def retry_pending(limit=PENDING_RETRY_BATCH):
    """Re-attempt whatever the last chain load could not finish, AND reseed
    whatever got subscribed but never got its first REST quote (the scan
    spent its inline SEED_BUDGET_PER_LOAD before reaching it, or the quote
    failed). Same mechanism as live_warrant.py's version, minus every
    DB-backed step (no terms, no name backfill, no MIS volume — none of
    that exists here)."""
    with _lock:
        if _stopped_by_user or not _connections:
            return {"subscribed": 0, "reseeded": 0, "pending": len(_pending)}
        todo = [c for c in _tracked if c in _pending][:limit]
        reseed = [c for c in _tracked if c not in _pending and c not in _seeded][:limit]

    subscribed = 0
    to_seed = []
    for code in todo:
        try:
            _subscribe_one(code)
            with _lock:
                _pending.discard(code)
            to_seed.append(code)
            subscribed += 1
        except Exception as e:
            detail = _error_detail(e)
            with _lock:
                _pending.add(code)
                _track_errors[code] = f"subscribe failed: {detail}"
            print(f"LIVEOPTIONS: retry subscribe {code} failed: {detail}", flush=True)

    reseeded = 0
    batch = to_seed + reseed
    if batch:
        with ThreadPoolExecutor(max_workers=SEED_WORKERS) as pool:
            results = list(pool.map(_reseed_one, batch))
        reseeded = sum(1 for r in results if r)

    with _lock:
        still = len(_pending)
    if subscribed or reseeded:
        print(f"LIVEOPTIONS: retry_pending subscribed={subscribed} reseeded={reseeded} pending={still}",
              flush=True)
    return {"subscribed": subscribed, "reseeded": reseeded, "pending": still}


def _teardown():
    """Close every pooled connection and log out. Identical shape to
    live_warrant.py's version."""
    with _lock:
        conns = list(_connections)
        _connections.clear()
        for conn in conns:
            conn["state"] = "closing"
        _pending.update(_tracked)
    for conn in conns:
        try:
            conn["ws"].disconnect()
        except Exception as e:
            print(f"LIVEOPTIONS: teardown disconnect failed: {e}", flush=True)
        try:
            conn["sdk"].logout()
        except Exception as e:
            print(f"LIVEOPTIONS: teardown logout failed: {e}", flush=True)


def reconnect():
    """Manual hard restart: tear down the whole pool, then reopen from
    whatever's in `_tracked` this process's lifetime."""
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
    """Manual disconnect: tear down the whole pool and stay stopped until
    connect_session(); also used by wsgi.py's shutdown hook."""
    global _stopped_by_user
    _stopped_by_user = True
    _teardown()


# ─────────────────────────────────────────────────────────────────────────────
# Contract discovery
# ─────────────────────────────────────────────────────────────────────────────
def _rest_client():
    """REST client for chain discovery — lazily opens connection 0 if the
    pool is still empty (e.g. the very first Load TSMC Chain click)."""
    conn = _ensure_connection(0)
    return conn["sdk"].marketdata.rest_client.futopt.intraday


def _option_tickers(rest, product):
    """REGULAR then AFTERHOURS — whichever actually has listed contracts
    right now. Same retry-session pattern already proven in
    scripts/fubon_quote_viewer.py::_option_session."""
    for session in ("REGULAR", "AFTERHOURS"):
        rows = (rest.tickers(type="OPTION", product=product, session=session) or {}).get("data") or []
        if rows:
            return session, rows
    return None, []


def load_chain(underlying=SUPPORTED_UNDERLYING):
    """Discover every currently-listed option contract on `underlying` and
    subscribe all of them.

    Idempotent/safe to re-run: ADD-ONLY. A contract already tracked is left
    alone; newly-listed contracts are added; nothing is ever removed. This
    is fine here (unlike live_warrant.py's ranked-replace `scan_underlying`)
    because it's a single, manually-triggered, whole-catalog action, not an
    unattended top-N scan that has to decide what "fell out of the ranking"
    means — a shrunk catalog response here just means fewer NEW contracts
    get added this run, never a false deletion. Re-click to pick up
    newly-listed contracts; use `remove_code` to clear one that's stuck.
    """
    global _diagnostic_logged
    if underlying != SUPPORTED_UNDERLYING:
        raise ValueError(f"only {SUPPORTED_UNDERLYING} is supported in v1")

    rest = _rest_client()
    products = (rest.products(type="OPTION") or {}).get("data") or []
    # Every product whose underlying is this stock, regardless of type — kept
    # separately from the filtered list below so a product that matches on
    # underlyingSymbol but gets excluded by the underlyingType=="S" check
    # (e.g. if the weekly-SSO product, CDO for TSMC, turns out to carry a
    # different type tag than the monthly CDA one) is visible in the
    # response instead of silently vanishing — see logic/live_options_logic.py's
    # module docstring on weekly SSOs being a separate product code.
    underlying_products = [{"symbol": p.get("symbol"), "underlyingType": p.get("underlyingType")}
                            for p in products if p.get("underlyingSymbol") == underlying]
    product_codes = [p["symbol"] for p in products
                      if p.get("underlyingSymbol") == underlying
                      and p.get("underlyingType") == "S" and p.get("symbol")]
    if not product_codes:
        raise RuntimeError(f"no OPTION product found for underlying {underlying} "
                            f"(saw {underlying_products})")
    # More than one product code is possible (e.g. standard + mini contracts,
    # or the monthly CDA + weekly CDO SSO products) — merge tickers from
    # every one found, don't assume exactly one.

    rows = []
    for product in product_codes:
        _session, prows = _option_tickers(rest, product)
        rows.extend(prows)
    if not rows:
        raise RuntimeError(f"no OPTION contracts listed for {underlying} "
                            f"(products {product_codes}, checked REGULAR + AFTERHOURS)")

    if not _diagnostic_logged:
        _diagnostic_logged = True
        # HIGH RISK diagnostic (see module + logic/live_options_logic.py
        # docstrings): confirms or corrects the strike/call-put field-shape
        # guess against a real account, once per process.
        print(f"LIVEOPTIONS: diagnostic — one raw tickers() row: "
              f"{json.dumps(rows[0], default=str, ensure_ascii=False)}", flush=True)

    parsed = [p for p in (live_options_logic.parse_contract(r) for r in rows) if p]
    parse_failures = len(rows) - len(parsed)

    with _lock:
        for p in parsed:
            _contracts[p["code"]] = p
        current_total = len(_tracked)
        new_codes = live_options_logic.new_contract_codes(_tracked, [p["code"] for p in parsed])
    live_warrant_logic.check_capacity(current_total, len(new_codes))

    with _lock:
        for code in new_codes:
            _tracked.append(code)
            _pending.add(code)
    failed = _track_many(new_codes, seed_budget=SEED_BUDGET_PER_LOAD)

    print(f"LIVEOPTIONS: load_chain {underlying} products={product_codes} "
          f"(underlying_products={underlying_products}) "
          f"chain={len(parsed)} +{len(new_codes)} failed={len(failed)} "
          f"parse_failures={parse_failures}", flush=True)
    return {
        "chain": len(parsed), "added": len(new_codes), "failed": len(failed),
        "product_codes": product_codes, "underlying_products": underlying_products,
        "parse_failures": parse_failures,
        "expiries": sorted({p["expiry"].isoformat() for p in parsed}),
    }


def remove_code(code):
    """Unsubscribe + drop from `_tracked`/`_contracts`/`_track_errors`. No DB
    call (no persistence — see module docstring). Does NOT blacklist the
    contract: the next Load TSMC Chain click re-adds it if still listed
    (add-only diffing in `load_chain`). This is for clearing a stuck/erroring
    subscription to retry it fresh, not a permanent block."""
    with _lock:
        if code in _tracked:
            _tracked.remove(code)
    _untrack_one(code)


# ─────────────────────────────────────────────────────────────────────────────
# Read path
# ─────────────────────────────────────────────────────────────────────────────
def get_data():
    """The cache as a JSON-able dict — what /live_options_data serves."""
    with _lock:
        conn_snapshot = [
            {"index": i, "state": c["state"], "subs": len(c["codes"]), "last_error": c["last_error"]}
            for i, c in enumerate(_connections)
        ]
        tracked_snapshot = list(_tracked)
        pending_count = len(_pending)
        error_kinds = sorted(set(_track_errors.values()))
        error_count = len(_track_errors)
        expiries = sorted({c["expiry"].isoformat() for c in _contracts.values() if c.get("expiry")})
    return {
        "connected": any(c["state"] == "connected" for c in conn_snapshot),
        "session_error": _session_error,
        "connections": conn_snapshot,
        "subs": sum(c["subs"] for c in conn_snapshot),
        "max_subs": live_warrant_logic.MAX_TOTAL_SUBS,
        "max_connections": live_warrant_logic.MAX_CONNECTIONS,
        "tracked": len(tracked_snapshot),
        "pending": pending_count,
        "errors": error_count,
        "error_kinds": error_kinds[:5],
        "expiries": expiries,
        "contracts": [_contract_payload(code) for code in tracked_snapshot],
    }


def snapshot_for_underlying(underlying=SUPPORTED_UNDERLYING):
    """Read-only cross-module accessor for Live Arb (services/live_arb.py) —
    see live_warrant.py's identical-purpose function. `underlying` is
    accepted for symmetry only: every tracked contract here already belongs
    to SUPPORTED_UNDERLYING (this module supports exactly one underlying
    today, see the module docstring)."""
    with _lock:
        seq = _tick_seq
        rows = []
        for code in _tracked:
            book = _books.get(code)
            contract = _contracts.get(code) or {}
            is_put = contract.get("is_put")
            best = (live_warrant_logic.best_level(book["bids"], book["asks"])
                    if book else live_warrant_logic.best_level([], []))
            rows.append({
                "code": code, "name": contract.get("name") or code,
                "type": None if is_put is None else ("Put" if is_put else "Call"),
                "strike": contract.get("strike"),
                "expiry": contract.get("expiry"),
                "best": best,
                # Raw book metadata, unrelated to the arb-matching fields
                # above — feeds live_arb_logic.latest_tick()'s "Last
                # received tick" debug line, not the matcher itself.
                "ts": book["ts"] if book else None,
                "src": book.get("src") if book else None,
            })
    return seq, rows


def _contract_payload(code):
    """One tracked contract's row — code/expiry/strike/is_put/name plus its
    live best bid/ask, no computed columns at all."""
    with _lock:
        book = _books.get(code)
        contract = _contracts.get(code) or {}
        pending = code in _pending
        error = _track_errors.get(code)
    base = {
        "code": code,
        "name": contract.get("name") or code,
        "expiry": contract["expiry"].isoformat() if contract.get("expiry") else None,
        "strike": contract.get("strike"),
        "is_put": contract.get("is_put"),
        "pending": pending,
        "error": error,
    }
    if book is None:
        return {**base, "best": live_warrant_logic.best_level([], []), "age": None, "src": None}
    return {
        **base,
        "best": live_warrant_logic.best_level(book["bids"], book["asks"]),
        "age": round((datetime.now(timezone.utc) - book["ts"]).total_seconds(), 1),
        "src": book.get("src"),
    }
