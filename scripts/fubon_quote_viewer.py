"""Standalone Fubon connectivity + live quote viewer — not part of the main
app. Run it directly (`python scripts/fubon_quote_viewer.py`), open
http://127.0.0.1:5099, and add warrant codes to see each one's name, best
bid, and best ask. Credentials come from .env (FUBON_ID, FUBON_PASSWORD,
FUBON_CERT_PATH, FUBON_CERT_PASSWORD).

Login happens once at process start and the SDK connection is kept alive for
the life of the process; the page polls the REST quote snapshot
(sdk.marketdata.rest_client.stock.intraday.quote) on every request/refresh,
so "real time" here means "refreshed every few seconds", not push-streamed.
"""
import os

from flask import Flask, redirect, request

app = Flask(__name__)

REFRESH_SECONDS = 3
_tracked = []  # warrant codes, in add order
_sdk = None
_login_error = None


def _load_dotenv():
    """Same minimal loader as services/db.py, duplicated to keep this script standalone."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv()


def _login():
    """Logs in once and initializes realtime/REST market data; returns (sdk, error)."""
    person_id = os.environ.get("FUBON_ID")
    password = os.environ.get("FUBON_PASSWORD")
    cert_path = os.environ.get("FUBON_CERT_PATH")
    cert_pass = os.environ.get("FUBON_CERT_PASSWORD")

    missing = [
        name for name, val in [
            ("FUBON_ID", person_id),
            ("FUBON_PASSWORD", password),
            ("FUBON_CERT_PATH", cert_path),
            ("FUBON_CERT_PASSWORD", cert_pass),
        ] if not val
    ]
    if missing:
        return None, f"Missing .env vars: {', '.join(missing)}"

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        return None, "fubon_neo is not installed (see docs/vendor/README.md for the vendored wheel)"

    try:
        sdk = FubonSDK()
        result = sdk.login(person_id, password, cert_path, cert_pass)
    except Exception as e:
        return None, f"login() raised: {e}"

    if not getattr(result, "is_success", False):
        return None, f"login failed: {getattr(result, 'message', result)}"

    sdk.init_realtime()
    return sdk, None


def _fetch_quote(code):
    """One warrant's name/best-bid/best-ask, or an error message."""
    try:
        data = _sdk.marketdata.rest_client.stock.intraday.quote(symbol=code)
    except Exception as e:
        return {"code": code, "error": str(e)}

    bids = data.get("bids") or []
    asks = data.get("asks") or []
    return {
        "code": code,
        "name": data.get("name", "-"),
        "best_bid": bids[0]["price"] if bids else "-",
        "best_ask": asks[0]["price"] if asks else "-",
    }


@app.route("/")
def index():
    global _sdk, _login_error
    if _sdk is None and _login_error is None:
        _sdk, _login_error = _login()

    if _login_error:
        return f"""
        <html><body style="font-family: sans-serif; padding: 2rem;">
          <h1 style="color: #c1121f;">Fubon connection: FAILED</h1>
          <p>{_login_error}</p>
        </body></html>
        """

    rows = ""
    for row in (_fetch_quote(code) for code in _tracked):
        if "error" in row:
            rows += f"""<tr><td>{row['code']}</td><td colspan="3" style="color:#c1121f;">{row['error']}</td>
              <td><a href="/remove/{row['code']}">remove</a></td></tr>"""
        else:
            rows += f"""<tr><td>{row['code']}</td><td>{row['name']}</td>
              <td>{row['best_bid']}</td><td>{row['best_ask']}</td>
              <td><a href="/remove/{row['code']}">remove</a></td></tr>"""

    return f"""
    <html>
    <head><meta http-equiv="refresh" content="{REFRESH_SECONDS}"></head>
    <body style="font-family: sans-serif; padding: 2rem;">
      <h1 style="color: #1a7f37;">Fubon connection: CONNECTED</h1>
      <form method="post" action="/add">
        <input name="code" placeholder="warrant code, e.g. 030273" required>
        <button type="submit">Add</button>
      </form>
      <table border="1" cellpadding="6" style="border-collapse: collapse; margin-top: 1rem;">
        <tr><th>Code</th><th>Name</th><th>Best Bid</th><th>Best Ask</th><th></th></tr>
        {rows or '<tr><td colspan="5">No codes tracked yet</td></tr>'}
      </table>
      <p style="color: #666;">Refreshes every {REFRESH_SECONDS}s.</p>
    </body>
    </html>
    """


@app.route("/add", methods=["POST"])
def add():
    code = request.form.get("code", "").strip()
    if code and code not in _tracked:
        _tracked.append(code)
    return redirect("/")


@app.route("/remove/<code>")
def remove(code):
    if code in _tracked:
        _tracked.remove(code)
    return redirect("/")


if __name__ == "__main__":
    app.run(port=5099, debug=False)
