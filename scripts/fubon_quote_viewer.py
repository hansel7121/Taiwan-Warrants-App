"""Standalone Fubon live order-book viewer — not part of the main app.

Run it directly (`python scripts/fubon_quote_viewer.py`), open
http://127.0.0.1:5099, and add warrant codes to watch each one's full
five-level bid/ask ladder with volume. Credentials come from .env
(FUBON_ID, FUBON_PASSWORD, FUBON_CERT_PATH, FUBON_CERT_PASSWORD).

Push, not poll: the process logs in once, opens ONE websocket, and subscribes
each code to Fugle's `books` channel, which pushes all five levels of both
sides on every book change. `_books` is the in-process cache those callbacks
write; the browser reads it as JSON from /data and repaints, so the page never
reloads. That shape — cache fed by a socket callback, browser reading the
cache — is the one the real feature is meant to grow into.

`books` rather than `trades`: trades only emits when a trade actually prints,
so an illiquid warrant looks dead on it, and it carries a last price rather
than a book. The one thing books does NOT carry is the instrument's name, so
that comes from a single REST lookup per code, cached forever (names never
change, and REST is capped at 300/min).

NOTE: the book only streams during TWSE hours (09:00-13:30 TPE, weekdays).
Outside them the socket still connects and subscribes, and the page says so,
but no levels arrive.
"""
import json
import os
import socket
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, request

app = Flask(__name__)

BOOKS_CHANNEL = "books"
LEVELS = 5

_lock = threading.Lock()
_books = {}    # code -> {"bids": [...], "asks": [...], "ts": datetime}
_names = {}    # code -> name, fetched once from REST
_tracked = []  # codes, in add order

_sdk = None
_stock = None
_login_error = None
_connected = False
_msg_count = 0


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
    if message.get("event") != "data" or message.get("channel") != BOOKS_CHANNEL:
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
        }


def _on_connect(*_a, **_k):
    global _connected
    _connected = True
    print("WS: connected", flush=True)


def _on_disconnect(*a, **k):
    global _connected
    _connected = False
    print(f"WS: disconnected args={a!r} kwargs={k!r}", flush=True)


def _on_error(*a, **k):
    print(f"WS: error args={a!r} kwargs={k!r}", flush=True)


def _login():
    """Log in, then open the one websocket this process keeps for its lifetime."""
    global _sdk, _stock, _login_error

    creds = {
        "FUBON_ID": os.environ.get("FUBON_ID"),
        "FUBON_PASSWORD": os.environ.get("FUBON_PASSWORD"),
        "FUBON_CERT_PATH": os.environ.get("FUBON_CERT_PATH"),
        "FUBON_CERT_PASSWORD": os.environ.get("FUBON_CERT_PASSWORD"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        _login_error = f"Missing .env vars: {', '.join(missing)}"
        return

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        _login_error = "fubon_neo is not installed"
        return

    try:
        sdk = FubonSDK()
        result = sdk.login(creds["FUBON_ID"], creds["FUBON_PASSWORD"],
                           creds["FUBON_CERT_PATH"], creds["FUBON_CERT_PASSWORD"])
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

    _sdk, _stock = sdk, stock


def _fetch_name(code):
    """One REST lookup per code, cached forever — the books channel carries no name."""
    if code in _names:
        return _names[code]
    try:
        data = _sdk.marketdata.rest_client.stock.intraday.ticker(symbol=code)
        _names[code] = data.get("name", "-")
    except Exception as e:
        _names[code] = f"({type(e).__name__})"
    return _names[code]


def _ladder(code):
    """One code's five levels a side, padded so the table always has LEVELS rows."""
    with _lock:
        book = _books.get(code)
    if book is None:
        return {"code": code, "name": _names.get(code, "-"), "rows": [], "age": None}

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
    }


@app.route("/data")
def data():
    """The cache as JSON — what the page repaints from, so it never reloads."""
    with _lock:
        count = _msg_count
    return jsonify({
        "connected": _connected,
        "messages": count,
        "books": [_ladder(code) for code in _tracked],
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
  .book { display: inline-block; vertical-align: top; margin: 0 1.5rem 1.5rem 0; }
  .book h3 { margin: 0 0 .4rem; font-size: 1rem; }
  table { border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { padding: .25rem .7rem; text-align: right; border-bottom: 1px solid #eee; }
  th { font-size: .7rem; text-transform: uppercase; color: #888; font-weight: 500; }
  .bid { color: #146b52; } .ask { color: #a83b2b; }
  tr.best td { font-weight: 700; }
  .lv { color: #aaa; font-size: .75rem; text-align: center; }
  .meta { color: #888; font-size: .8rem; margin-top: .3rem; }
  .up { color: #146b52; } .down { color: #c1121f; }
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

function render(d) {
  document.getElementById("status").innerHTML =
    "Socket: <b class='" + (d.connected ? "up'>open" : "down'>closed") + "</b>"
    + " &nbsp;|&nbsp; messages: <b>" + d.messages.toLocaleString() + "</b>";

  document.getElementById("books").innerHTML = d.books.map(function (b) {
    var body = b.rows.length === 0
      ? "<tr><td colspan=5 style='color:#888;text-align:center'>waiting for book…</td></tr>"
      : b.rows.map(function (r) {
          return "<tr class='" + (r.level === 1 ? "best" : "") + "'>"
            + "<td class='bid'>" + cell(r.bid_size) + "</td>"
            + "<td class='bid'>" + cell(r.bid) + "</td>"
            + "<td class='lv'>" + r.level + "</td>"
            + "<td class='ask'>" + cell(r.ask) + "</td>"
            + "<td class='ask'>" + cell(r.ask_size) + "</td></tr>";
        }).join("");

    return "<div class='book'><h3>" + b.code + " &nbsp;" + b.name + "</h3>"
      + "<table><tr><th>Bid Vol</th><th>Bid</th><th></th><th>Ask</th><th>Ask Vol</th></tr>"
      + body + "</table>"
      + "<div class='meta'>" + (b.age === null ? "no data yet" : "updated " + b.age + "s ago")
      + " &nbsp;<a href='/remove/" + b.code + "'>remove</a></div></div>";
  }).join("") || "<p>No codes tracked yet.</p>";
}

function poll() {
  fetch("/data").then(function (r) { return r.json(); }).then(render)
    .catch(function () { document.getElementById("status").textContent = "server unreachable"; });
}
poll();
setInterval(poll, 500);
</script>
</body></html>"""


@app.route("/add", methods=["POST"])
def add():
    code = request.form.get("code", "").strip()
    if code and code not in _tracked:
        _tracked.append(code)
        _fetch_name(code)
        _stock.subscribe({"channel": BOOKS_CHANNEL, "symbol": code})
        print(f"WS: subscribed {code}", flush=True)
    return redirect("/")


@app.route("/remove/<code>")
def remove(code):
    if code in _tracked:
        _tracked.remove(code)
        try:
            _stock.unsubscribe({"channel": BOOKS_CHANNEL, "symbol": code})
        except Exception as e:
            print(f"WS: unsubscribe {code} failed: {e}", flush=True)
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
