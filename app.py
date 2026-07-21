from flask import Flask, render_template, request, jsonify, Response, g
from services import applog
from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic
from services import scheduler
from services import auth
from services import db
from services import db_products
from services import store
from logic import arb_logic
from services.auth import require_auth
import os
import io
import json
import socket
import signal
import subprocess
import time
import threading
import webbrowser
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from scipy.interpolate import griddata

base_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(base_dir, "templates"),
    static_folder=os.path.join(base_dir, "static"),
)

app.json.sort_keys = False
# Re-read templates from disk on every request so index.html edits show up on a
# plain browser reload — no server restart needed. (Python edits still need one.)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Render pings /healthz constantly; static assets are noise too. Neither says
# anything about what the app is doing, so they stay out of the log.
_LOG_SKIP_PATHS = {"/healthz", "/favicon.ico"}
# Params worth a completion line's worth of context, in the order they read best.
_LOG_PARAMS = ("stock_codes", "option_type", "strategy", "kind", "period")
# The paths say what data work is behind them. Only the routes that do real data
# work are named here; anything else logs its bare path, as before.
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
    # g.user is set by require_auth inside the view, so the user is only known
    # by the time the request completes — not at before_request.
    user = getattr(g, "user", None)
    if user and user.get("email"):
        extra += f" user={user['email']}"
    if hasattr(g, "log_rows"):
        extra += f" rows={g.log_rows}"
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


@app.route("/")
def index():
    return render_template("index.html", **auth.public_config())


@app.route("/login")
def login():
    return render_template("login.html", **auth.public_config())


@app.route("/check_email", methods=["POST"])
def check_email():
    # Public pre-send allowlist check for the login page. Reuses auth._is_allowed
    # (service-role query + 60s cache). If Supabase is unconfigured, allow so
    # local no-auth dev isn't blocked.
    if not os.environ.get("SUPABASE_URL"):
        return jsonify({"allowed": True})
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    return jsonify({"allowed": auth._is_allowed(email)})


@app.route("/get_portfolio")
@require_auth
def get_portfolio():
    return jsonify(store.get_portfolio(g.user["id"]))


@app.route("/save_portfolio", methods=["POST"])
@require_auth
def save_portfolio():
    store.save_portfolio(g.user["id"], request.json or [])
    return jsonify({"ok": True})


@app.route("/close_quote", methods=["POST"])
@require_auth
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
    mode = d.get("mode")
    survivor = d.get("survivor")
    us_code = d.get("us_stock_code")
    out = {
        "ok": True, "survivor": survivor, "source": "model",
        "current_premium": None, "current_fx": None, "adr_ratio": None,
        "warrant_bid": None, "opt_last_usd": None,
    }

    # Live ADR premium + FX drive the option leg for US / TW-US trades.
    if mode in ("us", "twus") and us_code:
        try:
            s = us_options_logic.adr_premium_scenario(us_code, 30)
            out["current_premium"] = s.get("current_premium")
            out["current_fx"] = (s.get("fx") or {}).get("current_fx")
            out["adr_ratio"] = us_options_logic._adr_map().get(us_code, {}).get("adr_ratio")
        except Exception:
            pass

    try:
        if survivor == "warrant" and mode in ("direct", "us"):
            # A real warrant (not a TW option): CMoney has a live bid.
            code = d.get("warrant_code")
            res = warrant_logic.get_cmoney_prices([code]) if code else {}
            w = (res.get(code) or {}).get("Warrant") if res else None
            if w:
                bid = float(w.get("BuyPr1") or 0)
                if bid > 0:
                    out["warrant_bid"] = bid
                    out["source"] = "market"
        elif survivor == "option" and mode in ("us", "twus") and us_code:
            last = us_options_logic.us_option_last(
                us_code, d.get("opt_type"), float(d.get("opt_strike") or 0),
                d.get("opt_expiry_iso"), out["current_fx"], out["adr_ratio"],
            )
            if last:
                out["opt_last_usd"] = last
                out["source"] = "market"
    except Exception:
        pass

    return jsonify(out)


@app.route("/list_warrant_stocks")
@require_auth
def list_warrant_stocks():
    return jsonify(db_products.list_warrant_stocks())


@app.route("/add_warrant_stock", methods=["POST"])
@require_auth
def add_warrant_stock():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.add_warrant_stock(code, data.get("name"))
    return jsonify({"ok": True})


@app.route("/remove_warrant_stock", methods=["POST"])
@require_auth
def remove_warrant_stock():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.remove_warrant_stock(code)
    return jsonify({"ok": True})


@app.route("/lookup_warrant_stock")
@require_auth
def lookup_warrant_stock():
    code = (request.args.get("code") or "").strip()
    info = warrant_logic._universe().get(code)
    return jsonify({"code": code, "name": info.name if info else None})


@app.route("/list_tw_option_products")
@require_auth
def list_tw_option_products():
    return jsonify(db_products.list_tw_option_products())


@app.route("/add_tw_option_product", methods=["POST"])
@require_auth
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
def list_us_option_products():
    return jsonify(db_products.list_us_option_products())


@app.route("/add_us_option_product", methods=["POST"])
@require_auth
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
def remove_us_option_product():
    data = request.json or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    db_products.remove_us_option_product(code)
    us_options_logic.invalidate_adr_map_cache()
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

    df, error, meta = warrant_logic.read_warrant(
        stock_codes,
        option_type,
        min_days,
        max_days,
        min_leverage,
        max_tv_pct,
        min_volume,
    )
    if error and df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0, "error": error, **meta})
    applog.set_rows(len(df))
    return jsonify({"rows": df.to_dict(orient="records"), "count": len(df), **meta})


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
    df, error, meta = options_logic.read_tw_option(
        stock_codes, option_type, min_days, max_days, min_leverage, min_volume
    )
    if error and df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0, "error": error, **meta})
    # Use pandas JSON serialisation so NaN → null (browser JSON.parse rejects bare NaN)
    rows = json.loads(df.to_json(orient="records"))
    applog.set_rows(len(df))
    return jsonify({"rows": rows, "count": len(df), **meta})


@app.route("/read_us_option", methods=["POST"])
@require_auth
def read_us_option():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    min_days = data.get("min_days", 0)
    max_days = data.get("max_days", 365)
    min_volume = data.get("min_volume", 0)
    df, error, meta = us_options_logic.us_options_scan(stock_codes, option_type, min_days, max_days, min_volume)
    if error and df.empty:
        applog.set_rows(0)
        return jsonify({"rows": [], "count": 0, "error": error, **meta})
    rows = json.loads(df.to_json(orient="records"))
    applog.set_rows(len(df))
    return jsonify({"rows": rows, "count": len(df), **meta})


@app.route("/read_us_option_csv", methods=["POST"])
@require_auth
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

    xi = np.linspace(min(x), max(x), 60)
    yi = np.linspace(min(y), max(y), 60)
    xi_grid, yi_grid = np.meshgrid(xi, yi)
    zi = griddata((x, y), z, (xi_grid, yi_grid), method="linear")
    zi = np.where(np.isnan(zi), None, zi)

    return jsonify({
        "x": xi.tolist(),
        "y": yi.tolist(),
        "z": zi.tolist(),
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

    xi = np.linspace(min(x), max(x), 80)
    yi = np.linspace(min(y), max(y), 80)
    xi_grid, yi_grid = np.meshgrid(xi, yi)
    zi = griddata((x, y), z, (xi_grid, yi_grid), method="linear")
    zi = np.where(np.isnan(zi), None, zi)

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
            "x": xi.tolist(),
            "y": yi.tolist(),
            "z": zi.tolist(),
            "scatter_x": x,
            "scatter_y": y,
            "scatter_z": z,
            "codes": codes,
            "names": names,
            "highlight": highlight,
            **meta,
        }
    )


@app.route("/adr_premium", methods=["POST"])
@require_auth
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
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        applog.set_rows(len(rows))
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        applog.log("ARB", f"match_warrant_us_option failed: {e}")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_warrant_us_option_csv", methods=["POST"])
@require_auth
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
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        applog.set_rows(len(rows))
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        applog.log("ARB", f"match_tw_us_option failed: {e}")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_tw_us_option_csv", methods=["POST"])
@require_auth
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
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        as_of = min(
            (t for t in (warrant_logic.cache_as_of(stock_codes),
                         options_logic.data_as_of(stock_codes)) if t),
            default=None,
        )
        applog.set_rows(len(rows))
        as_of_iso = datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None
        return jsonify({"rows": rows, "count": len(rows), "as_of": as_of_iso})
    except Exception as e:
        applog.log("ARB", f"match_warrant_tw_option failed: {e}")
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/match_warrant_tw_option_csv", methods=["POST"])
@require_auth
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


# RENDER_GIT_* are injected by Render into the running service; absent locally.
_COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:7]
_BRANCH = os.environ.get("RENDER_GIT_BRANCH") or "dev"


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
    # Local dev entry point. Production runs via wsgi.py + gunicorn.
    # Scheduler is opt-in (default OFF); see wsgi.py. With it off the CMoney
    # key is fetched lazily on the first warrant request instead of prefetched.
    if os.environ.get("ENABLE_SCHEDULER") == "1":
        scheduler.start()
    else:
        print("SCHED: disabled (set ENABLE_SCHEDULER=1 to enable)", flush=True)
    print("Step 1: resolving port", flush=True)
    port = _resolve_port(int(os.environ.get("PORT", 5001)))
    print(f"Step 2: starting browser timer (port {port})", flush=True)
    if not os.environ.get("RENDER"):
        threading.Timer(1.5, lambda: open_browser(port)).start()
    print("Step 3: starting cmoney key prefetch", flush=True)
    warrant_logic.prefetch_cmoney_key()
    print("Step 4: starting flask", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
