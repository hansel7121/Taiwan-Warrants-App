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
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from logic import live_warrant_logic
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
MIS_HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://mis.twse.com.tw/stock/index.jsp"}

_lock = threading.RLock()
_books = {}       # code -> {"bids": [...], "asks": [...], "ts": datetime, "src": "ws"|"rest"}
_names = {}       # code -> name
_tracked = []     # codes, add order — mirrors live_warrant_tracked
_connections = [] # each: {"sdk", "ws", "codes": set(), "sub_ids": {}, "state", "last_error"}
_session_error = None  # set when the session can't even start (e.g. no credentials)


# ─────────────────────────────────────────────────────────────────────────────
# Login / connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def _login_new_connection(idx):
    """Log in fresh and open one websocket connection bound to pool index `idx`. Raises on any failure."""
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
        # Before connect(): the socket starts emitting as soon as it authenticates.
        ws.on("message", lambda raw, i=idx: _handle_message(i, raw))
        ws.on("connect", lambda *a, i=idx, **k: _on_connect(i))
        ws.on("disconnect", lambda *a, i=idx, **k: _on_disconnect(i))
        ws.on("error", lambda *a, i=idx, **k: _on_error(i, a, k))
        ws.connect()
    finally:
        try:
            os.unlink(local_cert_path)
        except OSError:
            pass

    return {"sdk": sdk, "ws": ws, "codes": set(), "sub_ids": {}, "state": "connecting", "last_error": None}


def _ensure_connection(idx):
    """Open pool connection `idx` if it doesn't exist yet — connections only ever grow by one."""
    with _lock:
        if idx < len(_connections):
            return _connections[idx]
    conn = _login_new_connection(idx)
    with _lock:
        _connections.append(conn)
    return conn


def _handle_message(idx, raw):
    """Fold one books message into the shared cache; the SDK hands us the raw frame."""
    import json
    try:
        message = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return

    event = message.get("event")
    if event != "data" or message.get("channel") != BOOKS_CHANNEL:
        _handle_control(idx, event, message)
        return

    data = message.get("data") or {}
    code = data.get("symbol")
    if not code:
        return
    with _lock:
        _books[code] = {
            "bids": data.get("bids") or [],
            "asks": data.get("asks") or [],
            "ts": datetime.now(timezone.utc),
            "src": "ws",
        }


def _handle_control(idx, event, message):
    """Track this connection's own subscription ids and errors — unsubscribe needs the id, not the symbol."""
    data = message.get("data") or {}
    with _lock:
        conn = _connections[idx]
        if event == "subscribed":
            conn["sub_ids"][data.get("symbol")] = data.get("id")
        elif event == "unsubscribed":
            conn["sub_ids"].pop(data.get("symbol"), None)
        elif event == "error":
            conn["last_error"] = f"{message.get('code', '?')}: {data.get('message', message)}"
            print(f"LIVEWARRANT: conn {idx} error {conn['last_error']}", flush=True)


def _on_connect(idx):
    with _lock:
        _connections[idx]["state"] = "connected"
    print(f"LIVEWARRANT: conn {idx} connected", flush=True)


def _on_disconnect(idx):
    with _lock:
        was_connected = _connections[idx]["state"] == "connected"
        _connections[idx]["state"] = "disconnected"
    print(f"LIVEWARRANT: conn {idx} disconnected", flush=True)
    if was_connected:
        threading.Thread(target=_reconnect_connection, args=(idx,), daemon=True).start()


def _on_error(idx, args, kwargs):
    with _lock:
        _connections[idx]["state"] = "error"
        _connections[idx]["last_error"] = "; ".join(str(x) for x in args) or repr(kwargs)
    print(f"LIVEWARRANT: conn {idx} error {args!r} {kwargs!r}", flush=True)


def _reconnect_connection(idx):
    """A single connection's own auto-heal: re-login and resubscribe its own codes.

    Other connections are untouched — a transient blip on one socket must not
    disturb the rest of the pool. Gives up after RECONNECT_RETRIES and leaves
    the connection in "error" state for the manual Reconnect (whole-session
    restart) to recover.
    """
    for attempt in range(1, RECONNECT_RETRIES + 1):
        with _lock:
            codes = set(_connections[idx]["codes"])
        try:
            new_conn = _login_new_connection(idx)
            with _lock:
                _connections[idx] = new_conn
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
        _connections[idx]["state"] = "error"
        _connections[idx]["last_error"] = "auto-reconnect exhausted retries"
    print(f"LIVEWARRANT: conn {idx} auto-reconnect exhausted retries", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Per-code subscribe/unsubscribe
# ─────────────────────────────────────────────────────────────────────────────
def _seed_from_rest(sdk, code):
    """One REST quote per added code: names the instrument and fills its first book.

    The books channel is update-only (pushes on change, no snapshot on
    subscribe), so a book with no recent activity would stay blank until a
    market maker requotes without this.
    """
    try:
        q = sdk.marketdata.rest_client.stock.intraday.quote(symbol=code)
    except Exception as e:
        with _lock:
            _names.setdefault(code, f"({type(e).__name__})")
        return

    with _lock:
        _names[code] = q.get("name") or _names.get(code) or "-"

    bids, asks = q.get("bids") or [], q.get("asks") or []
    if not bids and not asks:
        return

    stamp = q.get("lastUpdated")
    try:
        ts = datetime.fromtimestamp(stamp / 1_000_000, timezone.utc)
    except (TypeError, ValueError, OSError):
        ts = datetime.now(timezone.utc)
    with _lock:
        _books.setdefault(code, {"bids": bids, "asks": asks, "ts": ts, "src": "rest"})


def _track_one(code):
    """Subscribe one code to whichever pool connection has room, opening one if needed."""
    with _lock:
        counts = [len(c["codes"]) for c in _connections]
    idx = live_warrant_logic.assign_slot(counts)
    conn = _ensure_connection(idx)
    conn["ws"].subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
    with _lock:
        conn["codes"].add(code)
    _seed_from_rest(conn["sdk"], code)


def _untrack_one(code):
    """Unsubscribe one code from whichever connection holds it."""
    with _lock:
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
    """Idempotent: no-op if the pool already has connections; otherwise loads the persisted tracked list."""
    global _session_error
    with _lock:
        if _connections:
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
    for row in rows:
        code = row["code"]
        with _lock:
            _names[code] = row.get("name") or "-"
            _tracked.append(code)
        try:
            _track_one(code)
        except Exception as e:
            _session_error = f"{code}: {type(e).__name__}: {e}"
            print(f"LIVEWARRANT: track {code} failed: {e}", flush=True)


def _teardown():
    with _lock:
        conns = list(_connections)
        _connections.clear()
    for conn in conns:
        try:
            conn["ws"].disconnect()
            conn["sdk"].logout()
        except Exception as e:
            print(f"LIVEWARRANT: teardown failed: {e}", flush=True)


def reconnect():
    """Manual hard restart: tear down the whole pool, then reopen from the persisted tracked list."""
    _teardown()
    start_session()


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

    _track_one(code)
    with _lock:
        name = _names.get(code) or code
        _tracked.append(code)
    db_live_warrant.upsert_tracked(code, name, source="manual")


def remove_code(code):
    db_live_warrant.remove_tracked(code)
    with _lock:
        if code in _tracked:
            _tracked.remove(code)
        _names.pop(code, None)
    _untrack_one(code)


def scan_underlying(underlying, top_n):
    """Rank `underlying`'s warrants by traded volume and replace its previous scan rows with the top N."""
    _name, codes = _warrant_codes_for(underlying)
    if not codes:
        raise RuntimeError(f"no warrants found for {underlying}")

    vols = _mis_volumes(codes)
    ranked = sorted(vols.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    ranked_codes = [c for c, _v in ranked] if ranked and ranked[0][1] else codes[:top_n]

    existing_rows = db_live_warrant.list_tracked()
    with _lock:
        current_total = len(_tracked)
    to_add, to_remove = live_warrant_logic.plan_scan_replace(
        existing_rows, underlying, ranked_codes, current_total)

    for code in to_remove:
        remove_code(code)
    for code in to_add:
        _track_one(code)
        with _lock:
            name = _names.get(code) or code
            _tracked.append(code)
        db_live_warrant.upsert_tracked(code, name, source="scan", underlying=underlying)

    return {"added": to_add, "removed": to_remove}


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity ranking (adapted from scripts/fubon_quote_viewer.py)
# ─────────────────────────────────────────────────────────────────────────────
def _rest_client():
    with _lock:
        if not _connections:
            raise RuntimeError("live session not connected")
        return _connections[0]["sdk"].marketdata.rest_client.stock


def _warrant_codes_for(stock_code):
    """Every listed warrant on one underlying, matched by name (see the fuller
    docstring on the original in scripts/fubon_quote_viewer.py::_warrant_codes_for
    for why this can't be a plain prefix test)."""
    rest = _rest_client()
    name = (rest.intraday.ticker(symbol=stock_code) or {}).get("name")
    if not name:
        return name, []

    warrants = (rest.intraday.tickers(type="WARRANT", market="TSE") or {}).get("data") or []
    real = []
    for market in ("TSE", "OTC"):
        rows = (rest.intraday.tickers(type="EQUITY", market=market) or {}).get("data") or []
        real.extend(r.get("name") or "" for r in rows)

    longer = [n for n in real if len(n) > len(name) and n.startswith(name)]
    codes = [w["symbol"] for w in warrants
             if (w.get("name") or "").startswith(name)
             and not any((w.get("name") or "").startswith(n) for n in longer)]
    return name, codes


def _mis_volumes(codes):
    """Accumulated traded volume (張) per code, from TWSE MIS in bulk."""
    batches = [codes[i:i + MIS_BATCH] for i in range(0, len(codes), MIS_BATCH)]

    def one(batch):
        try:
            r = requests.get(MIS_URL, timeout=15, headers=MIS_HEADERS, params={
                "ex_ch": "|".join(f"tse_{c}.tw" for c in batch),
                "json": "1", "delay": "0"})
            return r.json().get("msgArray") or []
        except Exception as e:
            print(f"LIVEWARRANT: MIS batch failed: {e}", flush=True)
            return []

    out = {}
    with ThreadPoolExecutor(max_workers=MIS_WORKERS) as pool:
        for rows in pool.map(one, batches):
            for row in rows:
                try:
                    out[row.get("c")] = int(row.get("v") or 0)
                except (TypeError, ValueError):
                    continue
    return out


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
    return {
        "connected": any(c["state"] == "connected" for c in conn_snapshot),
        "session_error": _session_error,
        "connections": conn_snapshot,
        "subs": sum(c["subs"] for c in conn_snapshot),
        "max_subs": live_warrant_logic.MAX_TOTAL_SUBS,
        "max_connections": live_warrant_logic.MAX_CONNECTIONS,
        "books": [_ladder_payload(code) for code in tracked_snapshot],
    }


def _ladder_payload(code):
    with _lock:
        book = _books.get(code)
        name = _names.get(code, "-")
    if book is None:
        return {"code": code, "name": name, "rows": [], "age": None, "src": None}
    return {
        "code": code,
        "name": name,
        "rows": live_warrant_logic.ladder_rows(book["bids"], book["asks"]),
        "age": round((datetime.now(timezone.utc) - book["ts"]).total_seconds(), 1),
        "src": book.get("src"),
    }
