"""Standalone Fubon live order-book viewer — not part of the main app.

Run it directly (`python scripts/fubon_quote_viewer.py`) and open
http://127.0.0.1:5099. At startup it ranks one underlying's warrants by
traded volume and subscribes the busiest TOP_N (`_subscribe_top_liquid`);
more codes can be added by hand. Each shows its full five-level bid/ask
ladder with volume. Credentials come from Supabase (fubon_credentials table,
Fernet-encrypted — see services/broker/), selected by FUBON_CRED_LABEL
(defaults to "default"); run scripts/seed_fubon_credentials.py once to
populate a row from a local .env. FUBON_VIEWER_UNDERLYING and
FUBON_VIEWER_TOP_N override what gets picked.

Push, not poll: the process logs in once, opens ONE websocket, and subscribes
each code to Fugle's `books` channel, which pushes all five levels of both
sides on every book change. `_books` is the in-process cache those callbacks
write; the browser reads it as JSON from /data and repaints, so the page never
reloads. That shape — cache fed by a socket callback, browser reading the
cache — is the one the real feature is meant to grow into.

`books` rather than `trades`: trades only emits when a trade actually prints,
so an illiquid warrant looks dead on it, and it carries a last price rather
than a book. But books is update-only — it pushes on book *change* and sends
no snapshot on subscribe, so an illiquid warrant is still blank until someone
requotes. Hence one REST quote per added code (`_seed_from_rest`), which fills
the first book and supplies the name that books frames don't carry.

NOTE: the book only streams during TWSE hours (09:00-13:30 TPE, weekdays).
Outside them the socket still connects and subscribes, and the page says so,
but no levels arrive.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, redirect, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.broker import credentials as broker_credentials

app = Flask(__name__)

# Which fubon_credentials row (see services/broker/credentials.py) to log in
# with. Lets a second account/login be tested later without a code change.
FUBON_CRED_LABEL = os.environ.get("FUBON_CRED_LABEL", broker_credentials.DEFAULT_LABEL)

BOOKS_CHANNEL = "books"
LEVELS = 5

# Which underlying's warrants to rank at startup, and how many to subscribe.
UNDERLYING = os.environ.get("FUBON_VIEWER_UNDERLYING", "2330")
TOP_N = int(os.environ.get("FUBON_VIEWER_TOP_N", "5"))

# TWSE MIS serves accumulated volume for warrants in bulk. 100 symbols per
# call is the ceiling — 150 comes back rtcode 9999.
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_BATCH = 100
MIS_WORKERS = 6
MIS_HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://mis.twse.com.tw/stock/index.jsp"}

# Fugle's docs now claim 300 subscriptions per connection, 7 concurrent
# connections per account (2100 symbols in total) — up from an older
# 200/5/1000 figure. Re-measured against this account by driving one
# websocket to its limit with FUBON_VIEWER_STRESS_TARGET=2100: it accepted
# exactly 300 before erroring `1001: Subscription limit exceeded`, confirming
# the per-connection figure. The 7-connection total wasn't (and can't be)
# checked from here — this process only ever opens one socket — so the 2100
# total is taken on faith from the docs, not independently verified.
MAX_SUBS_PER_CONN = 300
MAX_CONNECTIONS = 7
SUBS_POLL_S = 15

_lock = threading.Lock()
_books = {}    # code -> {"bids": [...], "asks": [...], "ts": datetime}
_names = {}    # code -> name, fetched once from REST
_tracked = []  # codes, in add order

_sdk = None
_stock = None
_login_error = None
_connected = False
_msg_count = 0
_ranking = None    # status line while the startup liquidity scan runs
_sub_ids = {}      # code -> server subscription id; the server's own view
_subs_seen = False # has the server told us anything about subscriptions yet
_ops = 0           # bumped on every subscribe/unsubscribe confirmation
_query_ops = None  # _ops when the in-flight subscriptions query was sent
_last_error = None
_ws_state = "connecting"


def _load_dotenv():
    """Same minimal loader as services/db.py, duplicated to keep this script standalone."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def _handle_message(raw):
    """Fold one books message into the cache; the SDK hands us the raw frame text."""
    global _msg_count
    try:
        message = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return

    with _lock:
        _msg_count += 1

    # One callback receives auth replies, subscribe confirmations and pongs too,
    # so both filters are load-bearing, not defensive.
    event = message.get("event")
    if event != "data" or message.get("channel") != BOOKS_CHANNEL:
        _handle_control(event, message)
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


def _handle_control(event, message):
    """Track the non-data frames: subscription ids, the server's own count, errors.

    Unsubscribe takes the id the server assigned, not the symbol — sending a
    symbol comes back `1003 id should not be empty` and the subscription stays
    alive against the cap — so the ids from `subscribed` have to be kept.
    """
    global _last_error, _subs_seen, _ops, _query_ops
    data = message.get("data") or {}

    # subscribed/unsubscribed are the server confirming a change, and they
    # arrive immediately. They — not a follow-up query — are what moves the
    # count, so the display tracks an add or remove the moment it takes.
    if event == "subscribed":
        with _lock:
            _sub_ids[data.get("symbol")] = data.get("id")
            _subs_seen = True
            _ops += 1

    elif event == "unsubscribed":
        with _lock:
            _sub_ids.pop(data.get("symbol"), None)
            _subs_seen = True
            _ops += 1

    elif event == "subscriptions":
        # Periodic reconciliation: replace the whole view, so a subscription
        # dropped server-side disappears here too. Ignored when a change was
        # confirmed after the query went out — that reply predates the change
        # and would put the stale count back.
        rows = data if isinstance(data, list) else []
        with _lock:
            # Accept only a reply we asked for with nothing confirmed since:
            # anything else predates a change and would restore a stale count.
            if _query_ops is None or _query_ops != _ops:
                _query_ops = None
                return
            _query_ops = None
            _sub_ids.clear()
            for row in rows:
                if row.get("symbol") and row.get("id"):
                    _sub_ids[row["symbol"]] = row["id"]
            _subs_seen = True

    elif event == "error":
        _last_error = f"{message.get('code', '?')}: {data.get('message', message)}"
        print(f"WS: error {_last_error}", flush=True)


def _refresh_subs():
    """Ask the server to restate its subscription list, for periodic reconciliation."""
    global _query_ops
    try:
        if _stock is not None and _connected:
            with _lock:
                _query_ops = _ops
            _stock.subscriptions()
    except Exception as e:
        print(f"WS: subscriptions query failed: {e}", flush=True)


def _poll_subscriptions():
    """Keep the count honest on a timer, so a server-side drop shows up on its own."""
    while True:
        time.sleep(SUBS_POLL_S)
        _refresh_subs()


def _on_connect(*_a, **_k):
    global _connected, _ws_state
    _connected = True
    _ws_state = "connected"
    print("WS: connected", flush=True)


def _on_disconnect(*a, **k):
    global _connected, _ws_state
    _connected = False
    _ws_state = "disconnected"
    print(f"WS: disconnected args={a!r} kwargs={k!r}", flush=True)


def _on_error(*a, **k):
    global _ws_state, _last_error
    _ws_state = "error"
    _last_error = "; ".join(str(x) for x in a) or repr(k)
    print(f"WS: error args={a!r} kwargs={k!r}", flush=True)


def _login():
    """Log in, then open the one websocket this process keeps for its lifetime.

    Credentials come from Supabase (fubon_credentials table, encrypted at
    rest — see services/broker/), not .env; run scripts/seed_fubon_credentials.py
    once to populate a row. The cert is downloaded to a throwaway local temp
    file only for the duration of sdk.login(), since the SDK needs a filesystem
    path, then removed.
    """
    global _sdk, _stock, _login_error

    try:
        creds = broker_credentials.get_credential(FUBON_CRED_LABEL)
    except Exception as e:
        _login_error = f"Supabase credential lookup failed: {type(e).__name__}: {e}"
        return
    if creds is None:
        _login_error = f"No fubon_credentials row for label '{FUBON_CRED_LABEL}'"
        return
    missing = [k for k in ("fubon_id", "fubon_password", "cert_password") if not creds.get(k)]
    if missing:
        _login_error = f"fubon_credentials row '{FUBON_CRED_LABEL}' missing: {', '.join(missing)}"
        return
    if not creds.get("cert_path"):
        _login_error = f"fubon_credentials row '{FUBON_CRED_LABEL}' has no cert uploaded"
        return

    try:
        cert_bytes = broker_credentials.download_cert(FUBON_CRED_LABEL)
    except Exception as e:
        _login_error = f"cert download failed: {type(e).__name__}: {e}"
        return

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        _login_error = "fubon_neo is not installed"
        return

    cert_ext = creds["cert_path"].rsplit(".", 1)[-1] if "." in creds["cert_path"] else "p12"
    fd, local_cert_path = tempfile.mkstemp(suffix=f".{cert_ext}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cert_bytes)

        sdk = FubonSDK()
        result = sdk.login(creds["fubon_id"], creds["fubon_password"],
                           local_cert_path, creds["cert_password"])
        if not getattr(result, "is_success", False):
            _login_error = f"login failed: {getattr(result, 'message', result)}"
            return

        sdk.init_realtime()
        stock = sdk.marketdata.websocket_client.stock
        # Before connect(): the socket starts emitting as soon as it authenticates,
        # and anything bound afterwards misses what already arrived.
        stock.on("message", _handle_message)
        stock.on("connect", _on_connect)
        stock.on("disconnect", _on_disconnect)
        stock.on("error", _on_error)
        stock.connect()
    except Exception as e:
        _login_error = f"{type(e).__name__}: {e}"
        return
    finally:
        try:
            os.unlink(local_cert_path)
        except OSError:
            pass

    _sdk, _stock = sdk, stock


def _seed_from_rest(code):
    """One REST quote per added code: names the instrument and fills the first book.

    The books channel is update-only — it pushes on book change and sends no
    snapshot on subscribe. A liquid code repaints instantly, but an illiquid
    warrant whose book has not moved in half an hour would show nothing until
    a market maker requotes. This seeds the cache so a code has its book
    immediately; websocket frames then overwrite it.
    """
    try:
        q = _sdk.marketdata.rest_client.stock.intraday.quote(symbol=code)
    except Exception as e:
        _names.setdefault(code, f"({type(e).__name__})")
        return

    _names[code] = q.get("name") or _names.get(code) or "-"

    bids, asks = q.get("bids") or [], q.get("asks") or []
    if not bids and not asks:
        return

    # lastUpdated is the exchange's own microsecond stamp, so the age shown is
    # how stale the book actually is, not how long ago we fetched it.
    stamp = q.get("lastUpdated")
    try:
        ts = datetime.fromtimestamp(stamp / 1_000_000, timezone.utc)
    except (TypeError, ValueError, OSError):
        ts = datetime.now(timezone.utc)

    with _lock:
        _books.setdefault(code, {"bids": bids, "asks": asks, "ts": ts, "src": "rest"})


def _warrant_codes_for(stock_code):
    """Every listed warrant on one underlying, matched by name.

    Warrant names are "<underlying><issuer><serial>" (台積電群益5A售12) and
    Fugle's warrant list carries no underlying field, so the name is the only
    link. A plain prefix test leaks a longer-named stock's warrants into a
    shorter one (長榮 vs 長榮鋼), so disambiguate structurally, the same way
    logic/warrant_logic.py does: the warrant belongs to this underlying only
    if no *longer* real security name also prefixes it.
    """
    rest = _sdk.marketdata.rest_client.stock
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
            print(f"MIS: batch failed: {e}", flush=True)
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


def _subscribe_top_liquid():
    """Rank one underlying's warrants by traded volume and watch the busiest N.

    Runs in a thread at startup so the page is usable while it works. Fugle
    has no bulk quote for warrants (its snapshot endpoints are equity-only,
    and there are ~31k listed warrants against a 300/min REST cap), so volume
    comes from TWSE MIS instead. Picking by volume matters: an arbitrarily
    chosen warrant is usually one that trades a couple of lots a day and whose
    book barely moves.
    """
    global _ranking
    try:
        _ranking = f"finding the {TOP_N} most traded {UNDERLYING} warrants…"
        name, codes = _warrant_codes_for(UNDERLYING)
        if not codes:
            _ranking = f"no warrants found for {UNDERLYING}"
            return

        _ranking = f"ranking {len(codes)} {name} warrants by volume…"
        vols = _mis_volumes(codes)
        ranked = sorted(vols.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
        if not ranked or ranked[0][1] == 0:
            # MIS down or nothing traded yet — fall back to raw listing order
            # so the page still has something to show.
            _ranking = f"MIS ranking unavailable — subscribing {name} warrants in listing order…"
            for code in codes[:TOP_N]:
                _track(code)
                print(f"ORDER: {code}", flush=True)
            _ranking = None
            return

        for code, vol in ranked:
            _track(code)
            print(f"TOP: {code} {vol} lots", flush=True)
        _ranking = None
    except Exception as e:
        _ranking = f"liquidity scan failed: {type(e).__name__}: {e}"
        print(f"TOP: scan failed: {e}", flush=True)


STRESS_TARGET = int(os.environ.get("FUBON_VIEWER_STRESS_TARGET", "0"))
# Lets several instances (one process = one connection) each claim a disjoint
# slice of the warrant list, so a fleet of them can push the account-wide
# total rather than all fighting over the same first 300 codes.
STRESS_OFFSET = int(os.environ.get("FUBON_VIEWER_STRESS_OFFSET", "0"))
# When several instances run at once (one process = one connection), each
# independently ranking warrants against TWSE MIS overloads the public
# endpoint (observed: a full ~31k-code market scan run twice at once got
# every batch connection-reset). One instance computes the ranking and
# writes it here; the rest just read it.
STRESS_RANK_CACHE = os.environ.get("FUBON_VIEWER_STRESS_RANK_CACHE", "")

# "liquid" (default): rank the pool by traded volume via TWSE MIS, falling
# back to sorted code order if MIS is unreachable. "code": skip MIS entirely
# and always use sorted code order — for comparing against the liquidity-
# ranked run, or when MIS is known to be blocked (e.g. from a non-TW IP).
STRESS_MODE = os.environ.get("FUBON_VIEWER_STRESS_MODE", "liquid")

# Large-cap names with the deepest warrant chains — enough to comfortably
# clear STRESS_TARGET=2100 (2330/TSMC alone runs ~1200) while keeping the
# MIS scan a fraction of the size of a full-market pass over all ~31k codes.
STRESS_UNDERLYINGS = ["2330", "2317", "2454", "2881", "2882", "2891",
                      "2603", "3008", "2412", "1301", "2308", "2382"]


def _stress_ranked_codes():
    """Warrant codes across STRESS_UNDERLYINGS, with names — from cache if
    another instance already computed it, else scanned fresh (via the same
    _warrant_codes_for + _mis_volumes pair _subscribe_top_liquid uses for one
    underlying) and cached. Order depends on STRESS_MODE: "liquid" ranks by
    traded volume (MIS), "code" always uses sorted code order."""
    if STRESS_RANK_CACHE and os.path.exists(STRESS_RANK_CACHE):
        with open(STRESS_RANK_CACHE) as f:
            rows = json.load(f)
        for code, name in rows:
            _names[code] = name
        return [code for code, _ in rows]

    global _ranking
    rest = _sdk.marketdata.rest_client.stock
    all_warrants = (rest.intraday.tickers(type="WARRANT", market="TSE") or {}).get("data") or []
    name_by_code = {r["symbol"]: r["name"] for r in all_warrants if r.get("symbol") and r.get("name")}

    pool = set()
    for stock in STRESS_UNDERLYINGS:
        _ranking = f"stress test: finding warrants for {stock}…"
        _, codes = _warrant_codes_for(stock)
        pool.update(codes)
    for code in pool:
        if code in name_by_code:
            _names[code] = name_by_code[code]

    # Sorted (not set-order) so that, absent a shared cache file, independent
    # instances still agree on one ordering to slice by STRESS_OFFSET.
    pool = sorted(pool)

    if STRESS_MODE == "code":
        ranked = pool
    else:
        _ranking = f"stress test: ranking {len(pool)} warrants by volume…"
        vols = _mis_volumes(pool)
        if any(vols.values()):
            ranked = sorted(pool, key=lambda c: vols.get(c, 0), reverse=True)
        else:
            # MIS unavailable — fall back to the deterministic sorted order so
            # every instance still slices the same underlying list consistently.
            ranked = pool

    if STRESS_RANK_CACHE:
        tmp = STRESS_RANK_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump([[c, _names.get(c, "-")] for c in ranked], f)
        os.replace(tmp, STRESS_RANK_CACHE)

    return ranked


def _stress_subscribe():
    """One-off probe: subscribe as many warrant codes as it takes to hit the
    server's real per-connection cap (or STRESS_TARGET), to check the cap
    against the newly-published 7-connections x 300-subs figure.

    Only runs when FUBON_VIEWER_STRESS_TARGET is set. Ranked by traded volume
    (same MIS source as _subscribe_top_liquid) rather than raw listing order,
    so each slice actually has live books instead of dead warrants nobody has
    quoted today. Also REST-seeds each code (same _seed_from_rest _track
    normally calls) so a book shows its current snapshot immediately instead
    of sitting blank until the books channel happens to push a change.
    """
    global _ranking
    try:
        _ranking = "stress test: listing all warrants…"
        ranked = _stress_ranked_codes()
        codes = ranked[STRESS_OFFSET:STRESS_OFFSET + STRESS_TARGET]
        print(f"STRESS: pool of {len(codes)} warrant codes (by volume), "
              f"offset {STRESS_OFFSET}, target {STRESS_TARGET}", flush=True)

        for i, code in enumerate(codes, 1):
            if code in _tracked:
                continue
            _tracked.append(code)
            _stock.subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
            time.sleep(0.03)  # let subscribed/error frames land before checking
            with _lock:
                err = _last_error
            if err and "1001" in err:
                print(f"STRESS: capped at {i} attempts ({len(_sub_ids)} confirmed): {err}",
                      flush=True)
                _ranking = f"stress test: hit cap at {i} attempts — {err}"
                return
            _seed_from_rest(code)
            if i % 50 == 0:
                print(f"STRESS: {i}/{len(codes)} attempted, {len(_sub_ids)} confirmed", flush=True)
                _ranking = f"stress test: {i}/{len(codes)} attempted, {len(_sub_ids)} confirmed"

        print(f"STRESS: reached target, {len(_sub_ids)} confirmed, no error", flush=True)
        _ranking = f"stress test: reached target ({len(_sub_ids)}), no cap hit"
    except Exception as e:
        print(f"STRESS: failed: {type(e).__name__}: {e}", flush=True)
        _ranking = f"stress test failed: {e}"


def _ladder(code):
    """One code's five levels a side, padded so the table always has LEVELS rows."""
    with _lock:
        book = _books.get(code)
    if book is None:
        return {"code": code, "name": _names.get(code, "-"), "rows": [],
                "age": None, "src": None}

    rows = []
    for i in range(LEVELS):
        bid = book["bids"][i] if i < len(book["bids"]) else None
        ask = book["asks"][i] if i < len(book["asks"]) else None
        rows.append({
            "level": i + 1,
            "bid_size": bid["size"] if bid else None,
            "bid": bid["price"] if bid else None,
            "ask": ask["price"] if ask else None,
            "ask_size": ask["size"] if ask else None,
        })
    return {
        "code": code,
        "name": _names.get(code, "-"),
        "rows": rows,
        "age": round((datetime.now(timezone.utc) - book["ts"]).total_seconds(), 1),
        "src": book.get("src"),
    }


@app.route("/data")
def data():
    """The cache as JSON — what the page repaints from, so it never reloads."""
    with _lock:
        count = _msg_count
        subs = len(_sub_ids) if _subs_seen else None
    return jsonify({
        "connected": _connected,
        "state": "login failed" if _login_error else _ws_state,
        "messages": count,
        "ranking": _ranking,
        "error": _login_error or _last_error,
        # subs is None until the server's first reply — the page shows the
        # locally tracked figure then, marked as unconfirmed.
        "subs": subs if subs is not None else len(_tracked),
        "subs_confirmed": subs is not None,
        "max_subs": MAX_SUBS_PER_CONN,
        "connections": 1 if _connected else 0,
        "max_connections": MAX_CONNECTIONS,
        "books": [_ladder(code) for code in list(_tracked)],
    })


@app.route("/")
def index():
    if _login_error:
        return f"""<html><body style="font-family:sans-serif;padding:2rem;">
          <h1 style="color:#c1121f;">Fubon: FAILED</h1><p>{_login_error}</p></body></html>"""

    return """<html><head><title>Fubon live order book</title>
<style>
  body { font-family: sans-serif; padding: 2rem; }
  h1 { margin: 0 0 .3rem; }
  .status { color: #555; margin-bottom: 1rem; }
  #books { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 1rem 1.5rem; align-items: start; }
  .book { border: 1px solid #eee; border-radius: 6px; padding: .6rem .8rem; }
  .book h3 { margin: 0 0 .4rem; font-size: .85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  table { border-collapse: collapse; font-variant-numeric: tabular-nums; width: 100%; }
  th, td { padding: .25rem .7rem; text-align: right; border-bottom: 1px solid #eee; }
  th { font-size: .7rem; text-transform: uppercase; color: #888; font-weight: 500; }
  .bid { color: #146b52; } .ask { color: #a83b2b; }
  tr.best td { font-weight: 700; }
  .lv { color: #aaa; font-size: .75rem; text-align: center; }
  .meta { color: #888; font-size: .8rem; margin-top: .3rem; }
  .up { color: #146b52; } .down { color: #c1121f; }
  .status { padding: .5rem .8rem; background: #f6f6f4; border-radius: 5px;
            display: inline-block; }
  .dot { display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
         margin-right: .45rem; vertical-align: baseline; }
  .dot.ok { background: #146b52; } .dot.warn { background: #d9822b; }
  .dot.bad { background: #c1121f; }
</style></head>
<body>
  <h1>Fubon live order book</h1>
  <div class="status" id="status">connecting…</div>
  <form method="post" action="/add">
    <input name="code" placeholder="warrant code" required><button>Add</button>
  </form>
  <div id="books"></div>
  <p class="meta">Five levels a side, pushed over the websocket. Book streams
     09:00-13:30 TPE on weekdays.</p>

<script>
function cell(v) { return v === null ? "-" : v.toLocaleString(); }

// A REST-seeded book is a snapshot of a stale market, not a live push — say which.
function age(b) {
  var t = b.age < 90 ? b.age + "s" : Math.round(b.age / 60) + "m";
  return b.src === "rest" ? "snapshot, book " + t + " old" : "updated " + t + " ago";
}

// At a couple hundred tracked warrants, a full innerHTML rebuild of every
// book (5 rows x 5 cells each) on every poll thrashes the DOM badly enough
// to freeze the tab. Instead each book div is created once and kept; polls
// only touch the specific <td>/text nodes whose value actually changed, so
// idle books (most of them, most polls) cost nothing to redraw.
var bookEls = {};             // code -> {root, rows: [{bs,bp,ap,as}...], meta, waiting}
var lastText = new WeakMap(); // element -> last string written, to skip no-op writes

function setText(el, s) {
  if (lastText.get(el) !== s) {
    el.textContent = s;
    lastText.set(el, s);
  }
}

function buildBook(b) {
  var root = document.createElement("div");
  root.className = "book";

  var h3 = document.createElement("h3");
  root.appendChild(h3);

  var table = document.createElement("table");
  table.innerHTML = "<tr><th>Bid Vol</th><th>Bid</th><th></th><th>Ask</th><th>Ask Vol</th></tr>";

  var rows = [];
  for (var i = 0; i < 5; i++) {
    var tr = document.createElement("tr");
    var bs = document.createElement("td"); bs.className = "bid";
    var bp = document.createElement("td"); bp.className = "bid";
    var lv = document.createElement("td"); lv.className = "lv"; lv.textContent = i + 1;
    var ap = document.createElement("td"); ap.className = "ask";
    var as = document.createElement("td"); as.className = "ask";
    tr.appendChild(bs); tr.appendChild(bp); tr.appendChild(lv); tr.appendChild(ap); tr.appendChild(as);
    table.appendChild(tr);
    rows.push({ tr: tr, bs: bs, bp: bp, ap: ap, as: as });
  }
  root.appendChild(table);

  var meta = document.createElement("div");
  meta.className = "meta";
  var metaText = document.createTextNode("");
  var removeLink = document.createElement("a");
  removeLink.textContent = "remove";
  removeLink.href = "/remove/" + b.code;
  meta.appendChild(metaText);
  meta.appendChild(document.createTextNode("  "));
  meta.appendChild(removeLink);
  root.appendChild(meta);

  return { root: root, h3: h3, table: table, rows: rows, meta: metaText, name: null };
}

// Every book always renders all 5 rows (dashes if no data yet) so books
// stay a uniform height and line up cleanly across the grid.
function patchBook(el, b, idx) {
  var title = (idx + 1) + ". " + b.code + "  " + b.name;
  if (el.name !== title) { el.h3.textContent = title; el.name = title; }

  for (var i = 0; i < el.rows.length; i++) {
    var r = b.rows[i], slot = el.rows[i];
    slot.tr.className = (r && r.level === 1) ? "best" : "";
    setText(slot.bs, cell(r ? r.bid_size : null));
    setText(slot.bp, cell(r ? r.bid : null));
    setText(slot.ap, cell(r ? r.ask : null));
    setText(slot.as, cell(r ? r.ask_size : null));
  }

  var metaStr = b.age === null ? "no data yet" : age(b);
  if (el.meta.data !== metaStr) el.meta.data = metaStr;
}

function render(d) {
  var dot = d.state === "connected" ? "ok" : (d.state === "connecting" ? "warn" : "bad");
  var subs = d.subs + "/" + d.max_subs + (d.subs_confirmed ? "" : "?");
  var statusHtml = "<span class='dot " + dot + "'></span><b>" + d.state + "</b>"
    + " &nbsp;|&nbsp; subscriptions <b>" + subs + "</b>"
    + " &nbsp;|&nbsp; connections <b>" + d.connections + "/" + d.max_connections + "</b>"
    + " &nbsp;|&nbsp; messages <b>" + d.messages.toLocaleString() + "</b>"
    + (d.ranking ? " &nbsp;|&nbsp; " + d.ranking : "")
    + (d.error ? " &nbsp;|&nbsp; <span class='down'>" + d.error + "</span>" : "");
  var statusEl = document.getElementById("status");
  if (statusEl._last !== statusHtml) { statusEl.innerHTML = statusHtml; statusEl._last = statusHtml; }

  var container = document.getElementById("books");
  var seen = {};
  d.books.forEach(function (b, idx) {
    seen[b.code] = true;
    var el = bookEls[b.code];
    if (!el) {
      el = bookEls[b.code] = buildBook(b);
      container.appendChild(el.root);
    }
    patchBook(el, b, idx);
  });

  // Drop books no longer tracked (removed via /remove) so the DOM doesn't
  // accumulate stale nodes as codes are added/removed over a session.
  Object.keys(bookEls).forEach(function (code) {
    if (!seen[code]) {
      container.removeChild(bookEls[code].root);
      delete bookEls[code];
    }
  });

  if (d.books.length === 0 && !container.querySelector("p")) {
    container.innerHTML = "<p>No codes tracked yet.</p>";
  } else if (d.books.length > 0) {
    var stray = container.querySelector("p");
    if (stray) container.removeChild(stray);
  }
}

// Guard against overlapping requests: at high subscription counts the /data
// payload is large enough that a slow fetch could still be in flight when
// the next timer fires, and stacking fetches only makes things worse.
var inFlight = false;
function poll() {
  if (inFlight) return;
  inFlight = true;
  fetch("/data").then(function (r) { return r.json(); }).then(function (d) {
    inFlight = false;
    render(d);
  }).catch(function () {
    inFlight = false;
    document.getElementById("status").textContent = "server unreachable";
  });
}
poll();
setInterval(poll, 500);
</script>
</body></html>"""


def _track(code):
    """Subscribe one code and seed its book — the one path into _tracked."""
    if not code or code in _tracked:
        return
    _tracked.append(code)
    # Subscribe first, then seed: a frame arriving during the REST call is
    # newer than the snapshot, and _seed_from_rest won't overwrite it.
    _stock.subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
    print(f"WS: subscribed {code}", flush=True)
    _seed_from_rest(code)


@app.route("/add", methods=["POST"])
def add():
    _track(request.form.get("code", "").strip())
    return redirect("/")


@app.route("/remove/<code>")
def remove(code):
    if code in _tracked:
        _tracked.remove(code)
        # By id, not symbol: a symbol gets `1003 id should not be empty` back
        # and the subscription keeps running against the cap.
        with _lock:
            sub_id = _sub_ids.get(code)
        try:
            if sub_id:
                _stock.unsubscribe({"id": sub_id})
            else:
                print(f"WS: no subscription id for {code}, cannot unsubscribe", flush=True)
        except Exception as e:
            print(f"WS: unsubscribe {code} failed: {e}", flush=True)
        # _sub_ids is left to the server's `unsubscribed` confirmation, so the
        # count only drops once the subscription is actually gone.
        with _lock:
            _books.pop(code, None)
    return redirect("/")


def _claim_port(port):
    """Hold the port before login, so a clash can't strand a broker session.

    Fubon allows only 5 concurrent connections and login happens before
    app.run() binds, so without this a port clash exits with a live session
    still open at the broker, and a few retries burn the quota.
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


if __name__ == "__main__":
    PORT = int(os.environ.get("FUBON_VIEWER_PORT", "5099"))
    held = _claim_port(PORT)
    if held is None:
        raise SystemExit(
            f"Port {PORT} is already in use — another copy is probably still running.\n"
            f"  lsof -i :{PORT}   # find it\n"
            f"  kill <PID>        # stop it\n"
            f"Or: FUBON_VIEWER_PORT=5100 python scripts/fubon_quote_viewer.py"
        )

    _login()
    if _login_error:
        print(f"LOGIN FAILED: {_login_error}", flush=True)
    else:
        # Threaded: the scan sweeps ~1200 codes over TWSE MIS, so the page
        # should come up and say what it's doing rather than wait for it.
        if STRESS_TARGET:
            threading.Thread(target=_stress_subscribe, daemon=True).start()
        else:
            threading.Thread(target=_subscribe_top_liquid, daemon=True).start()
        threading.Thread(target=_poll_subscriptions, daemon=True).start()

    held.close()  # released immediately before app.run() rebinds it
    try:
        app.run(port=PORT, debug=False)
    finally:
        if _sdk is not None:
            try:
                _stock.disconnect()
                _sdk.logout()
                print("WS: logged out", flush=True)
            except Exception as e:
                print(f"WS: logout failed: {e}", flush=True)
