# Flask routes only: parse requests, call logic/ and services/, return JSON/CSV.
# Also owns request logging (before/after/teardown hooks) and the JSON error handler.
from flask import Flask, render_template, request, jsonify, Response, g
from werkzeug.exceptions import HTTPException
from services import applog
from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic
from services import scheduler
from services import auth
from services import db
from services import db_products
from services import db_suggestions
from services import db_user
from services import roles
from services import store
from services import live_warrant
from logic import arb_logic
from logic import live_warrant_logic
from logic import static_arb
# Aliased: the route functions below are named iv_surface / close_quote.
from logic import iv_surface as iv_surface_logic
from logic import iv_engine
from logic import close_quote as close_quote_logic
from logic import user_marks
from services.auth import require_auth
from services.roles import require_role, ADMIN
import os
import io
import json
import socket
import signal
import subprocess
import time
import threading
import traceback
import webbrowser
import pandas as pd
from datetime import datetime, timezone

base_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(base_dir, "templates"),
    static_folder=os.path.join(base_dir, "static"),
)

app.json.sort_keys = False
# Templates re-read on every request so index.html edits show up on reload.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Render's proxy doesn't gzip; compress here since JSON tables compress well.
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    print("flask-compress not installed; responses served uncompressed")

# Health checks and static assets are noise; keep them out of the request log.
_LOG_SKIP_PATHS = {"/healthz", "/favicon.ico"}
_LOG_PARAMS = ("stock_codes", "option_type", "strategy", "kind", "period")
_ROUTE_LABELS = {
    "/read_warrant": "warrants",
    "/read_warrant_csv": "warrants csv",
    "/read_tw_option": "tw options",
    "/read_tw_option_csv": "tw options csv",
    "/read_us_option_csv": "us options csv",
    "/read_us_option": "us options",
    "/iv_surface": "iv surface",
    "/iv_surface_options": "iv surface (options)",
    "/adr_premium": "adr premium",
    "/adr_premium_scenario": "adr premium scenario",
    "/match_warrant_us_option": "us/tw match",
    "/match_warrant_us_option_csv": "us/tw match csv",
    "/match_tw_us_option": "tw/us match",
    "/match_tw_us_option_csv": "tw/us match csv",
    "/match_warrant_tw_option": "arb scan",
    "/match_warrant_tw_option_csv": "arb scan csv",
    "/match_static_arb": "static arb lp",
    "/match_static_arb_csv": "static arb lp csv",
}


def _log_skip():
    p = request.path
    return p in _LOG_SKIP_PATHS or p.startswith("/static/")


def _route_label():
    """' (warrants)' for a named route, '' for anything else."""
    label = _ROUTE_LABELS.get(request.path)
    return f" ({label})" if label else ""


def _param_summary():
    """Short 'key=value' digest of the request payload — never the whole body."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return ""
        parts = []
        for key in _LOG_PARAMS:
            if key not in data:
                continue
            val = data[key]
            if isinstance(val, list):
                val = ",".join(str(v) for v in val[:6]) + ("+" if len(val) > 6 else "")
            parts.append(f"{key.replace('stock_codes', 'codes')}={val}")
        return " " + " ".join(parts) if parts else ""
    except Exception:
        return ""


@app.before_request
def _log_request_start():
    if _log_skip():
        return
    g.log_id = applog.new_id()
    g.log_t0 = time.time()
    applog.log(
        "REQ",
        f"{request.method} {request.path}{_route_label()} start{_param_summary()}",
    )


def _log_request_end(status):
    if getattr(g, "log_done", False) or not hasattr(g, "log_id"):
        return
    g.log_done = True
    extra = ""
    # g.user is only known once the view runs, not at before_request.
    user = getattr(g, "user", None)
    if user and user.get("email"):
        extra += f" user={user['email']}"
    if hasattr(g, "log_rows"):
        extra += f" rows={g.log_rows}"
    stages = getattr(g, "log_stages", None)
    if stages:
        extra += " " + " ".join(f"{k}={v * 1000:.1f}ms" for k, v in stages.items())
    applog.log(
        "REQ",
        f"{request.method} {request.path}{_route_label()} {status} "
        f"in {time.time() - getattr(g, 'log_t0', time.time()):.1f}s{extra}",
    )


@app.after_request
def _log_request_ok(response):
    _log_request_end(response.status_code)
    return response


@app.teardown_request
def _log_request_teardown(exc):
    # after_request is skipped when a view raises; this is the only hook that
    # always runs, so an unhandled error still gets its completion line.
    if exc is not None:
        _log_request_end(f"EXC {type(exc).__name__}: {exc}")


def _rows_json(df, **extra):
    """`{"rows": [...], "count": n, ...}` built around pandas' JSON text verbatim.

    The obvious `jsonify({"rows": json.loads(df.to_json(...))})` serializes the
    frame, parses it back into Python objects, and serializes it a second time —
    ~20 ms on a 725-row frame, two thirds of it pure waste. Splicing the text
    pandas already produced keeps the `rows` bytes byte-identical (same
    `to_json`, same `double_precision=10`, same NaN -> null) at a third of the
    cost. Only the envelope's key order changes: `rows` first, then `count` and
    the caller's extras, where `jsonify` sorted them alphabetically.

    `to_json`, not `to_dict`: unquoted warrants carry NaN in every ask-derived
    column, and `jsonify` would emit a bare NaN literal that `JSON.parse` rejects.
    """
    t0 = time.perf_counter()
    applog.set_rows(len(df))
    rows_json = df.to_json(orient="records") if not df.empty else "[]"
    envelope = json.dumps({"count": len(df), **extra})[1:]  # drop the leading brace
    body = '{"rows":' + rows_json + "," + envelope
    applog.add_stage("enc", time.perf_counter() - t0)
    return Response(body, mimetype="application/json")


@app.errorhandler(Exception)
def _json_error(exc):
    """Fail as JSON on API routes — the frontend parses every response body.

    Without this, an unhandled exception returns Flask's HTML error page and the
    browser reports "Unexpected token '<'", which says nothing about the actual
    failure. Page routes keep their normal HTML error.
    """
    code = exc.code if isinstance(exc, HTTPException) else 500
    if request.path in ("/", "/login"):
        raise exc
    if not isinstance(exc, HTTPException):
        applog.log("REQ", f"unhandled {type(exc).__name__}: {exc}\n"
                          f"{traceback.format_exc()}", level="ERROR")
    return jsonify({"error": str(exc)}), code


@app.route("/")
def index():
    # roles.template_flags() decides which tabs and script tags the page even
    # contains — user mode never receives the arb/portfolio markup or JS.
    return render_template("index.html", **auth.public_config(), **roles.template_flags())


@app.route("/login")
def login():
    return render_template("login.html", **auth.public_config())


@app.route("/check_email", methods=["POST"])
def check_email():
    # Unconfigured Supabase means local no-auth dev, so allow.
    if not os.environ.get("SUPABASE_URL"):
        return jsonify({"allowed": True})
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    return jsonify({"allowed": auth._is_allowed(email)})


@app.route("/get_portfolio")
@require_auth
@require_role(ADMIN)
def get_portfolio():
    return jsonify(store.get_portfolio(g.user["id"]))


@app.route("/save_portfolio", methods=["POST"])
@require_auth
@require_role(ADMIN)
def save_portfolio():
    store.save_portfolio(g.user["id"], request.json or [])
    return jsonify({"ok": True})


@app.route("/close_quote", methods=["POST"])
@require_auth
@require_role(ADMIN)
def close_quote():
    """Current market inputs used to book a trade's realized P&L at close.

    Returns the live ADR premium / FX (basis for the option leg) and a real
    market quote for the *surviving* (long-dated) leg where one can be fetched:
    a warrant's current bid from CMoney, or a US ADR option's last price from
    yfinance. Anything else falls back to source="model" and the frontend
    values that leg with Black-Scholes instead. The near (expired) leg always
    settles at intrinsic, so it needs no quote here.
    """
    d = request.json or {}
    survivor = d.get("survivor")
    quote = close_quote_logic.resolve_survivor(
        d.get("mode"), survivor,
        warrant_code=d.get("warrant_code"),
        us_code=d.get("us_stock_code"),
        opt_type=d.get("opt_type"),
        opt_strike=d.get("opt_strike"),
        opt_expiry_iso=d.get("opt_expiry_iso"),
    )
    return jsonify({"ok": True, "survivor": survivor, **quote})


@app.route("/list_warrant_stocks")
@require_auth
def list_warrant_stocks():
    return jsonify(db_products.list_warrant_stocks())


@app.route("/add_warrant_stock", methods=["POST"])
@require_auth
@require_role(ADMIN)
def add_warrant_stock():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.add_warrant_stock(code, data.get("name"))
    return jsonify({"ok": True})


@app.route("/remove_warrant_stock", methods=["POST"])
@require_auth
@require_role(ADMIN)
def remove_warrant_stock():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.remove_warrant_stock(code)
    return jsonify({"ok": True})


@app.route("/lookup_warrant_stock")
@require_auth
@require_role(ADMIN)
def lookup_warrant_stock():
    code = (request.args.get("code") or "").strip()
    info = warrant_logic._universe().get(code)
    return jsonify({"code": code, "name": info.name if info else None})


# ── User dashboard: watchlist / alerts / positions ───────────────────
# Every route below scopes to g.user["id"]; user_id never comes from the request body.

_WATCH_KINDS = {"warrant", "tw_option"}
_LEG_KINDS = {"warrant", "tw_option", "underlying"}
_ALERT_METRICS = {"bid", "ask", "iv", "underlying"}
_LEG_FIELDS = ("kind", "code", "label", "direction", "quantity", "entry_price",
               "option_type", "strike", "days_to_expiry", "exercise_ratio",
               "contract_size", "iv")


@app.route("/list_watchlist")
@require_auth
def list_watchlist():
    return jsonify(db_user.list_watchlist(g.user["id"]))


@app.route("/add_watchlist", methods=["POST"])
@require_auth
def add_watchlist():
    d = request.json or {}
    kind, code = d.get("kind"), (d.get("code") or "").strip()
    if kind not in _WATCH_KINDS or not code:
        return jsonify({"error": "kind and code required"}), 400
    db_user.add_watchlist(g.user["id"], kind, code,
                          underlying_code=d.get("underlying_code"),
                          label=d.get("label"), meta=d.get("meta") or {})
    return jsonify({"ok": True})


@app.route("/remove_watchlist", methods=["POST"])
@require_auth
def remove_watchlist():
    d = request.json or {}
    kind, code = d.get("kind"), (d.get("code") or "").strip()
    if kind not in _WATCH_KINDS or not code:
        return jsonify({"error": "kind and code required"}), 400
    db_user.remove_watchlist(g.user["id"], kind, code)
    return jsonify({"ok": True})


@app.route("/list_alerts")
@require_auth
def list_alerts():
    return jsonify(db_user.list_alerts(g.user["id"]))


@app.route("/add_alert", methods=["POST"])
@require_auth
def add_alert():
    d = request.json or {}
    kind, code = d.get("kind"), (d.get("code") or "").strip()
    metric, direction = d.get("metric"), d.get("direction")
    if kind not in _WATCH_KINDS or not code:
        return jsonify({"error": "kind and code required"}), 400
    if metric not in _ALERT_METRICS or direction not in ("above", "below"):
        return jsonify({"error": "bad metric/direction"}), 400
    try:
        threshold = float(d.get("threshold"))
    except (TypeError, ValueError):
        return jsonify({"error": "threshold must be a number"}), 400
    row = db_user.add_alert(g.user["id"], kind, code, metric, direction, threshold,
                            underlying_code=d.get("underlying_code"), note=d.get("note"))
    return jsonify({"ok": True, "alert": row})


@app.route("/remove_alert", methods=["POST"])
@require_auth
def remove_alert():
    alert_id = (request.json or {}).get("id")
    if not alert_id:
        return jsonify({"error": "id required"}), 400
    db_user.remove_alert(g.user["id"], alert_id)
    return jsonify({"ok": True})


@app.route("/record_alert_trigger", methods=["POST"])
@require_auth
def record_alert_trigger():
    """Persist a browser-side alert fire so it survives a reload."""
    d = request.json or {}
    alert_id = d.get("id")
    if not alert_id:
        return jsonify({"error": "id required"}), 400
    fired_at = datetime.now(timezone.utc).isoformat()
    db_user.record_trigger(g.user["id"], alert_id, d.get("value"), fired_at)
    return jsonify({"ok": True, "last_triggered_at": fired_at})


@app.route("/list_positions")
@require_auth
def list_positions():
    return jsonify(db_user.list_positions(g.user["id"]))


@app.route("/add_position", methods=["POST"])
@require_auth
def add_position():
    """Create a multi-leg position. At least one leg; no cap on leg count."""
    d = request.json or {}
    raw_legs = d.get("legs") or []
    if not isinstance(raw_legs, list) or not raw_legs:
        return jsonify({"error": "at least one leg required"}), 400
    legs = []
    for raw in raw_legs:
        kind = raw.get("kind")
        code = (raw.get("code") or "").strip()
        if kind not in _LEG_KINDS or not code:
            return jsonify({"error": f"bad leg: {raw}"}), 400
        try:
            quantity = float(raw.get("quantity"))
            entry_price = float(raw.get("entry_price"))
            direction = int(raw.get("direction", 1))
        except (TypeError, ValueError):
            return jsonify({"error": f"leg needs numeric quantity/entry_price: {code}"}), 400
        if direction not in (-1, 1) or quantity <= 0:
            return jsonify({"error": f"leg direction must be ±1 and quantity > 0: {code}"}), 400
        # Warrants are long-only; a short warrant leg can't actually be held.
        if kind == "warrant" and direction != 1:
            return jsonify({"error": f"warrants are long-only, cannot short {code}"}), 400
        leg = {k: raw.get(k) for k in _LEG_FIELDS}
        leg.update({"kind": kind, "code": code, "quantity": quantity,
                    "entry_price": entry_price, "direction": direction})
        if leg.get("option_type") not in ("Call", "Put"):
            leg["option_type"] = None
        legs.append(leg)
    position = db_user.add_position(
        g.user["id"], legs, name=d.get("name"),
        underlying_code=d.get("underlying_code"), note=d.get("note"))
    return jsonify({"ok": True, "position": position})


@app.route("/remove_position", methods=["POST"])
@require_auth
def remove_position():
    position_id = (request.json or {}).get("id")
    if not position_id:
        return jsonify({"error": "id required"}), 400
    db_user.remove_position(g.user["id"], position_id)
    return jsonify({"ok": True})


@app.route("/close_position", methods=["POST"])
@require_auth
def close_position():
    position_id = (request.json or {}).get("id")
    if not position_id:
        return jsonify({"error": "id required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    db_user.close_position(g.user["id"], position_id, now)
    return jsonify({"ok": True, "closed_at": now})


@app.route("/user_quotes", methods=["POST"])
@require_auth
def user_quotes():
    """Current bid/ask/IV/spot for the caller's watchlist + position legs."""
    instruments = (request.json or {}).get("instruments") or []
    try:
        marks = user_marks.quotes(instruments)
    except Exception as e:
        applog.log("USER", f"user_quotes failed: {e}\n{traceback.format_exc()}", level="ERROR")
        return jsonify({"quotes": {}, "error": str(e)})
    return jsonify({"quotes": marks})


@app.route("/list_suggestions")
@require_auth
@require_role(ADMIN)
def list_suggestions():
    return jsonify(db_suggestions.list_active_suggestions())


@app.route("/remove_suggestion", methods=["POST"])
@require_auth
@require_role(ADMIN)
def remove_suggestion():
    data = request.json or {}
    sug_id = (data.get("id") or "").strip()
    if not sug_id:
        return jsonify({"error": "id required"}), 400
    db_suggestions.delete_suggestion(sug_id)
    return jsonify({"ok": True})


@app.route("/clear_suggestions", methods=["POST"])
@require_auth
@require_role(ADMIN)
def clear_suggestions():
    db_suggestions.clear_active_suggestions()
    return jsonify({"ok": True})


@app.route("/list_tw_option_products")
@require_auth
def list_tw_option_products():
    return jsonify(db_products.list_tw_option_products())


@app.route("/add_tw_option_product", methods=["POST"])
@require_auth
@require_role(ADMIN)
def add_tw_option_product():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    commodity_ids = data.get("commodity_ids")
    ticker = (data.get("ticker") or "").strip()
    exercise_ratio = data.get("exercise_ratio")
    if not code or not commodity_ids or not ticker or not exercise_ratio:
        return jsonify({"error": "code, commodity_ids, ticker, exercise_ratio required"}), 400
    if isinstance(commodity_ids, str):
        commodity_ids = [c.strip() for c in commodity_ids.split(",") if c.strip()]
    db_products.add_tw_option_product(code, commodity_ids, ticker, int(exercise_ratio), data.get("name"))
    options_logic.invalidate_commodity_map_cache()
    return jsonify({"ok": True})


@app.route("/remove_tw_option_product", methods=["POST"])
@require_auth
@require_role(ADMIN)
def remove_tw_option_product():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.remove_tw_option_product(code)
    options_logic.invalidate_commodity_map_cache()
    return jsonify({"ok": True})


@app.route("/list_us_option_products")
@require_auth
@require_role(ADMIN)
def list_us_option_products():
    return jsonify(db_products.list_us_option_products())


@app.route("/add_us_option_product", methods=["POST"])
@require_auth
@require_role(ADMIN)
def add_us_option_product():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    adr_ticker = (data.get("adr_ticker") or "").strip()
    fx_ticker = (data.get("fx_ticker") or "").strip()
    adr_ratio = data.get("adr_ratio")
    if not code or not adr_ticker or not fx_ticker or not adr_ratio:
        return jsonify({"error": "code, adr_ticker, fx_ticker, adr_ratio required"}), 400
    db_products.add_us_option_product(code, adr_ticker, fx_ticker, float(adr_ratio), data.get("name"))
    us_options_logic.invalidate_adr_map_cache()
    return jsonify({"ok": True})


@app.route("/remove_us_option_product", methods=["POST"])
@require_auth
@require_role(ADMIN)
def remove_us_option_product():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.remove_us_option_product(code)
    us_options_logic.invalidate_adr_map_cache()
    return jsonify({"ok": True})


@app.route("/live_warrant_data")
@require_auth
@require_role(ADMIN)
def live_warrant_data():
    return jsonify(live_warrant.get_data())


@app.route("/add_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def add_live_warrant():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    try:
        live_warrant.add_code(code)
    except live_warrant_logic.CapacityExceededError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/remove_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def remove_live_warrant():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    live_warrant.remove_code(code)
    return jsonify({"ok": True})


@app.route("/scan_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def scan_live_warrant():
    data = request.json or {}
    underlying = (data.get("underlying") or "").strip()
    top_n = data.get("top_n")
    if not underlying or not top_n:
        return jsonify({"error": "underlying, top_n required"}), 400
    try:
        result = live_warrant.scan_underlying(underlying, int(top_n))
    except live_warrant_logic.CapacityExceededError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **result})


@app.route("/reconnect_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def reconnect_live_warrant():
    live_warrant.reconnect()
    return jsonify({"ok": True})


@app.route("/connect_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def connect_live_warrant():
    live_warrant.connect_session()
    return jsonify({"ok": True})


@app.route("/disconnect_live_warrant", methods=["POST"])
@require_auth
@require_role(ADMIN)
def disconnect_live_warrant():
    live_warrant.stop_session()
    return jsonify({"ok": True})


@app.route("/read_warrant", methods=["POST"])
@require_auth
def read_warrant():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    try:
        df, error, meta = warrant_logic.read_warrant(
            stock_codes,
            option_type,
            min_days,
            max_days,
            min_leverage,
            max_tv_pct,
            min_volume,
            # The scanner shows every book state — one-sided and empty included.
            allow_no_quote=True,
        )
    except Exception as e:
        applog.log("WARR", f"read_warrant failed: {e}\n{traceback.format_exc()}", level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})
    # Only a genuine fetch failure is an error. An empty result with no
    # hard_error means the filters simply passed nothing through, which the
    # frontend must render neutrally.
    hard_error = meta.pop("hard_error", None)
    if df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0,
                        "error": error if hard_error else None, **meta})
    return _rows_json(df, **meta)


@app.route("/read_warrant_csv", methods=["POST"])
@require_auth
def read_warrant_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    df, error, _meta = warrant_logic.read_warrant(
        stock_codes,
        option_type,
        min_days,
        max_days,
        min_leverage,
        max_tv_pct,
        min_volume,
        allow_no_quote=True,
    )
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=warrants.csv"},
    )


@app.route("/read_tw_option", methods=["POST"])
@require_auth
def read_tw_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["TXO"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    min_volume = int(data.get("min_volume", 0))
    try:
        df, error, meta = options_logic.read_tw_option(
            stock_codes, option_type, min_days, max_days, min_leverage, min_volume
        )
    except Exception as e:
        applog.log("OPT", f"read_tw_option failed: {e}\n{traceback.format_exc()}", level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})
    hard_error = meta.pop("hard_error", None)
    if df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0,
                        "error": error if hard_error else None, **meta})
    return _rows_json(df, **meta)


@app.route("/read_us_option", methods=["POST"])
@require_auth
@require_role(ADMIN)
def read_us_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    min_days = data.get("min_days", 0)
    max_days = data.get("max_days", 365)
    min_volume = data.get("min_volume", 0)
    try:
        df, error, meta = us_options_logic.us_options_scan(stock_codes, option_type, min_days, max_days, min_volume)
    except Exception as e:
        applog.log("USOPT", f"read_us_option failed: {e}\n{traceback.format_exc()}", level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})
    hard_error = meta.pop("hard_error", None)
    if df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0,
                        "error": error if hard_error else None, **meta})
    return _rows_json(df, **meta)


@app.route("/read_us_option_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def read_us_option_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    min_days = data.get("min_days", 0)
    max_days = data.get("max_days", 365)
    min_volume = data.get("min_volume", 0)
    df, _error, _meta = us_options_logic.us_options_scan(stock_codes, option_type, min_days, max_days, min_volume)
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=us_options.csv"},
    )


@app.route("/read_tw_option_csv", methods=["POST"])
@require_auth
def read_tw_option_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["TXO"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    min_volume = int(data.get("min_volume", 0))
    df, _error, _meta = options_logic.read_tw_option(
        stock_codes, option_type, min_days, max_days, min_leverage, min_volume
    )
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=options.csv"},
    )


@app.route("/iv_surface_options", methods=["POST"])
@require_auth
def iv_surface_options():
    data = request.json
    stock_codes = data.get("stock_codes", ["TXO"])
    option_type = data.get("option_type", "Call")
    df, error, _meta = options_logic.read_tw_option(stock_codes, option_type, min_days=1)
    if df.empty:
        return jsonify({"error": error or "No data"})

    df = df[df["iv_ask"].notna() & (df["iv_ask"] > 0)]
    if len(df) < 4:
        return jsonify({"error": "Not enough data points to build a surface."})

    x = df["strike"].values.tolist()
    y = df["days_to_expiry"].values.tolist()
    z = (df["iv_ask"].values * 100).tolist()
    labels = df["contract"].tolist()

    xi, yi, zi = iv_surface_logic.interpolate_grid(x, y, z, resolution=80)

    return jsonify({
        "x": xi,
        "y": yi,
        "z": zi,
        "scatter_x": x,
        "scatter_y": y,
        "scatter_z": z,
        "labels": labels,
    })


@app.route("/iv_surface", methods=["POST"])
@require_auth
def iv_surface():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    highlight_code = data.get("highlight_code", "").strip()

    df_clean, error, meta = warrant_logic.fetch_iv_surface(stock_codes, option_type)
    if df_clean is None:
        return jsonify({"error": error, **meta})

    x = df_clean["strike"].values.tolist()
    y = df_clean["days_to_expiry"].values.tolist()
    z = (df_clean["iv_ask"].values * 100).tolist()
    codes = df_clean["warrant_code"].astype(str).tolist()
    names = df_clean["warrant_name"].tolist()

    xi, yi, zi = iv_surface_logic.interpolate_grid(x, y, z, resolution=80)

    highlight = None
    if highlight_code:
        mask = df_clean["warrant_code"].astype(str) == highlight_code
        if mask.any():
            row = df_clean[mask].iloc[0]
            highlight = {
                "x": float(row["strike"]),
                "y": int(row["days_to_expiry"]),
                "z": float(row["iv_ask"] * 100),
                "code": highlight_code,
            }

    return jsonify(
        {
            "x": xi,
            "y": yi,
            "z": zi,
            "scatter_x": x,
            "scatter_y": y,
            "scatter_z": z,
            "codes": codes,
            "names": names,
            "highlight": highlight,
            **meta,
        }
    )


@app.route("/universe_status")
def universe_status():
    # Read-only progress check; never triggers a scrape itself.
    return jsonify(warrant_logic.universe_status())


@app.route("/adr_premium", methods=["POST"])
@require_auth
@require_role(ADMIN)
def adr_premium():
    data = request.json
    stock_code = data.get("stock_code")
    try:
        if stock_code not in us_options_logic._adr_map():
            raise RuntimeError(f"{stock_code}: no US ADR mapping")
        return jsonify(us_options_logic.adr_premium_stats(stock_code))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/adr_premium_scenario", methods=["POST"])
@require_auth
@require_role(ADMIN)
def adr_premium_scenario():
    data = request.json
    stock_code = data.get("stock_code")
    horizon_days = int(data.get("horizon_days", 30) or 30)
    try:
        if stock_code not in us_options_logic._adr_map():
            raise RuntimeError(f"{stock_code}: no US ADR mapping")
        return jsonify(us_options_logic.adr_premium_scenario(stock_code, horizon_days))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/match_warrant_us_option", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_warrant_us_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = arb_logic.match_warrant_us_option(stock_codes, option_type, max_strike_diff_pct,
                                max_dte_diff, positive_loose=positive_loose,
                                min_volume=min_volume, strategy=strategy)
        return _rows_json(df)
    except arb_logic.NoMatchesError:
        # A clean scan that matched nothing renders as "no rows", not an error.
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0})
    except Exception as e:
        applog.log("ARB", f"match_warrant_us_option failed: {e}\n{traceback.format_exc()}",
                   level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_warrant_us_option_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_warrant_us_option_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = arb_logic.match_warrant_us_option(stock_codes, option_type, max_strike_diff_pct,
                                max_dte_diff, positive_loose=positive_loose,
                                min_volume=min_volume, strategy=strategy)
    except arb_logic.NoMatchesError:
        # Clean scan, nothing matched — download an empty CSV, not an error.
        df = pd.DataFrame()
    except Exception:
        df = pd.DataFrame()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=us_option_match.csv"},
    )


@app.route("/match_tw_us_option", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_tw_us_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = arb_logic.match_tw_us_option(stock_codes, option_type, max_strike_diff_pct,
                                    max_dte_diff, positive_loose=positive_loose,
                                    min_volume=min_volume)
        return _rows_json(df)
    except arb_logic.NoMatchesError:
        # Clean scan that matched nothing — a normal outcome, not an error.
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0})
    except Exception as e:
        applog.log("ARB", f"match_tw_us_option failed: {e}\n{traceback.format_exc()}",
                   level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_tw_us_option_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_tw_us_option_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = arb_logic.match_tw_us_option(stock_codes, option_type, max_strike_diff_pct,
                                    max_dte_diff, positive_loose=positive_loose,
                                    min_volume=min_volume)
    except arb_logic.NoMatchesError:
        # Clean scan, nothing matched — download an empty CSV, not an error.
        df = pd.DataFrame()
    except Exception:
        df = pd.DataFrame()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tw_us_option_match.csv"},
    )


@app.route("/match_warrant_tw_option", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_warrant_tw_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = arb_logic.match_warrant_tw_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                           positive_loose=positive_loose, min_volume=min_volume,
                           strategy=strategy)
        as_of = min(
            (t for t in (warrant_logic.cache_as_of(stock_codes),
                         options_logic.data_as_of(stock_codes)) if t),
            default=None,
        )
        as_of_iso = datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None
        return _rows_json(df, as_of=as_of_iso)
    except arb_logic.NoMatchesError:
        # Same shape as a successful zero-row scan, as_of included.
        as_of = min(
            (t for t in (warrant_logic.cache_as_of(stock_codes),
                         options_logic.data_as_of(stock_codes)) if t),
            default=None,
        )
        applog.set_rows(0)
        as_of_iso = datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None
        return jsonify({"rows": [], "count": 0, "as_of": as_of_iso})
    except Exception as e:
        applog.log("ARB", f"match_warrant_tw_option failed: {e}\n{traceback.format_exc()}",
                   level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_warrant_tw_option_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_warrant_tw_option_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = arb_logic.match_warrant_tw_option(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                           positive_loose=positive_loose, min_volume=min_volume,
                           strategy=strategy)
    except arb_logic.NoMatchesError:
        # Clean scan, nothing matched — download an empty CSV, not an error.
        df = pd.DataFrame()
    except Exception:
        df = pd.DataFrame()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=arb_finder.csv"},
    )


def _static_arb_params(data):
    return dict(
        stock_codes=data.get("stock_codes", ["2330"]),
        min_volume=int(data.get("min_volume", 0) or 0),
        min_edge=float(data.get("min_edge", 0) or 0),
        max_horizon_dte=int(data.get("max_horizon_dte") or 0) or None,
        # Absent = the original buy-only Experiment sub-tab, unchanged.
        allow_short_warrants=bool(data.get("allow_short_warrants", False)),
    )


def _static_arb_as_of(stock_codes):
    return min(
        (t for t in (warrant_logic.cache_as_of(stock_codes),
                     options_logic.data_as_of(stock_codes)) if t),
        default=None,
    )


@app.route("/match_static_arb", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_static_arb():
    data = request.json
    params = _static_arb_params(data)
    stock_codes = params["stock_codes"]
    try:
        df = static_arb.match_static_arb(**params)
        # to_json would stringify the nested per-leg dicts; to_dict keeps them.
        rows = df.to_dict(orient="records") if not df.empty else []
        applog.set_rows(len(rows))
        as_of = _static_arb_as_of(stock_codes)
        return jsonify({
            "rows": rows, "count": len(rows),
            "dropped_no_depth": int(df.attrs.get("dropped_no_depth", 0)),
            "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None,
        })
    except arb_logic.NoMatchesError as e:
        # Clean scan that found nothing. The message carries why (including any
        # legs dropped for missing resting size), which is the whole difference
        # between "chain is arb-free" and "TAIFEX MIS was down".
        applog.set_rows(0)
        as_of = _static_arb_as_of(stock_codes)
        return jsonify({
            "rows": [], "count": 0, "note": str(e),
            "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None,
        })
    except Exception as e:
        applog.log("ARB", f"match_static_arb failed: {e}\n{traceback.format_exc()}",
                   level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_static_arb_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def match_static_arb_csv():
    """One CSV row per LEG, keyed by structure_id — a structure has a variable
    number of legs, so it cannot be flattened into fixed columns."""
    try:
        df = static_arb.match_static_arb(**_static_arb_params(request.json))
    except Exception:
        df = pd.DataFrame()
    flat = []
    for i, r in enumerate(df.to_dict(orient="records") if not df.empty else []):
        head = {k: v for k, v in r.items() if k != "legs"}
        for leg in r["legs"]:
            flat.append({"structure_id": i, **head,
                         **{f"leg_{k}": v for k, v in leg.items()}})
    output = io.StringIO()
    pd.DataFrame(flat).to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=static_arb.csv"},
    )


def _straddle_params(data):
    return dict(
        stock_codes=data.get("stock_codes", ["2330"]),
        option_type=data.get("option_type", "All"),
        max_strike_diff_pct=float(data.get("max_strike_diff_pct", 10.0)),
        max_dte_diff=int(data.get("max_dte_diff", 30)),
        min_volume=int(data.get("min_volume", 0) or 0),
        min_iv_edge=float(data.get("min_iv_edge", 1.0)),
        loose=bool(data.get("loose", False)),
        short_warrants=bool(data.get("short_warrants", False)),
        require_dte_cover=bool(data.get("require_dte_cover", True)),
    )


@app.route("/straddle_arbitrage", methods=["POST"])
@require_auth
@require_role(ADMIN)
def straddle_arbitrage():
    p = _straddle_params(request.json)
    try:
        df = arb_logic.build_straddle_arb(**p)
        as_of = min(
            (t for t in (warrant_logic.cache_as_of(p["stock_codes"]),
                         options_logic.data_as_of(p["stock_codes"])) if t),
            default=None,
        )
        as_of_iso = datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None
        return _rows_json(df, as_of=as_of_iso)
    except Exception as e:
        applog.log("ARB", f"straddle_arbitrage failed: {e}\n{traceback.format_exc()}",
                   level="ERROR")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/straddle_arbitrage_csv", methods=["POST"])
@require_auth
@require_role(ADMIN)
def straddle_arbitrage_csv():
    p = _straddle_params(request.json)
    try:
        df = arb_logic.build_straddle_arb(**p)
        # Flatten the nested leg dicts so the CSV is human-readable.
        flat = []
        for r in json.loads(df.to_json(orient="records")):
            row = {k: v for k, v in r.items() if not isinstance(v, dict)}
            for lk in ("long_call", "long_put", "short_call", "short_put"):
                lg = r.get(lk) or {}
                row[f"{lk}"] = f"{lg.get('source')} {lg.get('id')} K{lg.get('K')} {lg.get('dte')}d iv{lg.get('iv')}"
            flat.append(row)
        df = pd.DataFrame(flat)
    except Exception:
        df = pd.DataFrame()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=straddle_arbitrage.csv"},
    )


# SOURCE_COMMIT is Coolify's build-time equivalent of Render's RENDER_GIT_COMMIT;
# absent locally and until the Hetzner/Coolify deploy passes it through.
_COMMIT = (os.environ.get("SOURCE_COMMIT") or "dev")[:7]
_BRANCH = os.environ.get("SOURCE_BRANCH") or "dev"


def _asset_version():
    """Cache-busting stamp appended to static asset URLs (?v=...).

    On Render this is the deploy's commit SHA, so a new deploy invalidates every
    cached JS/CSS file. Locally it falls back to the newest static-file mtime
    (edits bump it), then to process start time if static/ is missing.
    """
    if _COMMIT != "dev":
        return _COMMIT
    try:
        return str(int(max(
            os.path.getmtime(os.path.join(r, f))
            for r, _, fs in os.walk(os.path.join(base_dir, "static"))
            for f in fs
        )))
    except (ValueError, OSError):
        return str(int(time.time()))


ASSET_VERSION = _asset_version()


@app.context_processor
def _inject_asset_version():
    return {"ASSET_VERSION": ASSET_VERSION}


@app.route("/healthz")
def healthz():
    return jsonify(
        {
            "status": "ok",
            "commit": _COMMIT,
            "branch": _BRANCH,
            "scheduler": os.environ.get("ENABLE_SCHEDULER") == "1",
            "iv_engine": iv_engine.engine_info(),
        }
    )


@app.route("/sync_warrant", methods=["POST"])
@require_auth
def sync_warrant():
    return jsonify(scheduler.force_refresh("warrants"))


@app.route("/sync_tw_option", methods=["POST"])
@require_auth
def sync_tw_option():
    return jsonify(scheduler.force_refresh("tw_options"))


@app.route("/sync_us_option", methods=["POST"])
@require_auth
def sync_us_option():
    return jsonify(scheduler.force_refresh("us_options"))


@app.route("/sync_universe", methods=["POST"])
@require_auth
@require_role(ADMIN)
def sync_universe():
    return jsonify(scheduler.force_refresh("universe"))


def open_browser(port):
    webbrowser.open(f"http://127.0.0.1:{port}")


def _port_free(port):
    """True if we can bind the port right now (nothing listening on it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _pids_listening(port):
    """PIDs LISTENing on the port (macOS/Linux via lsof). Empty on any error."""
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return [int(x) for x in out.split()]
    except Exception:
        return []


def _is_stale_self(pid):
    """True if `pid` is another instance of THIS app (its command line runs
    app.py), so it is safe to reclaim the port from it. Never our own PID."""
    if pid == os.getpid():
        return False
    try:
        cmd = subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        )
        return "app.py" in cmd or "app.spec" in cmd
    except Exception:
        return False


def _resolve_port(preferred, span=20):
    """Return a bindable port. If the preferred one is held by a *stale instance
    of this same app*, kill it and reclaim the port (the usual cause of
    'Address already in use' after a restart). If it's held by something else,
    fall back to the next free port instead of fighting over it."""
    if _port_free(preferred):
        return preferred
    reclaimed = False
    for pid in _pids_listening(preferred):
        if _is_stale_self(pid):
            print(f"Port {preferred} held by stale instance pid {pid} — killing it", flush=True)
            try:
                os.kill(pid, signal.SIGTERM)
                reclaimed = True
            except Exception:
                pass
    if reclaimed:
        for _ in range(30):          # up to ~3s for the socket to release
            if _port_free(preferred):
                return preferred
            time.sleep(0.1)
    for p in range(preferred + 1, preferred + span):   # else next free port
        if _port_free(p):
            print(f"Port {preferred} busy (not ours) — using {p} instead", flush=True)
            return p
    return preferred                 # give up; let app.run surface the error


if __name__ == "__main__":
    # Local dev entry point; production runs via wsgi.py + gunicorn.
    if os.environ.get("ENABLE_SCHEDULER") == "1":
        scheduler.start()
    else:
        print("SCHED: disabled (set ENABLE_SCHEDULER=1 to enable)", flush=True)
    print("Step 1: resolving port", flush=True)
    port = _resolve_port(int(os.environ.get("PORT", 5001)))
    print(f"Step 2: starting browser timer (port {port})", flush=True)
    if os.environ.get("APP_ENV") != "production":
        threading.Timer(1.5, lambda: open_browser(port)).start()
    print("Step 3: starting cmoney key prefetch", flush=True)
    warrant_logic.prefetch_cmoney_key()
    print("Step 4: starting flask", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
