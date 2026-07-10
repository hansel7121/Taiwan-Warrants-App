from flask import Flask, render_template, request, jsonify, Response, g
import warrant_logic
import options_logic
import us_options_logic
import scheduler
import auth
import db
from auth import require_auth
import os
import json
import numpy as np
import pandas as pd
from datetime import datetime
from scipy.interpolate import griddata


def _epoch_iso(ts):
    return datetime.fromtimestamp(ts).isoformat() if ts else None

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
    return jsonify(db.get_portfolio(g.user["id"]))


@app.route("/save_portfolio", methods=["POST"])
@require_auth
def save_portfolio():
    db.save_portfolio(g.user["id"], request.json or [])
    return jsonify({"ok": True})


def _us_option_last(us_code, opt_type, strike_twd, expiry_iso, fx, adr_ratio):
    """Last traded price (USD per ADR) of the surviving US option leg.

    Matches the ADR option chain by nearest expiry then nearest strike. The
    stored strike is TWD-per-TW-share, so it is converted back to a USD/ADR
    strike (× adr_ratio ÷ FX) before matching. Returns None if anything is
    missing so the caller can fall back to a model value.
    """
    import yfinance as yf
    cfg = us_options_logic.US_ADR_MAP.get(us_code)
    if not cfg or not fx or not adr_ratio or strike_twd <= 0:
        return None
    strike_usd = strike_twd * adr_ratio / fx
    tk = yf.Ticker(cfg["adr_ticker"])
    exps = tk.options
    if not exps:
        return None
    if expiry_iso:
        target = pd.Timestamp(expiry_iso).normalize()
        exp = min(exps, key=lambda e: abs((pd.Timestamp(e) - target).days))
    else:
        exp = exps[0]
    chain = tk.option_chain(exp)
    leg = chain.calls if str(opt_type).lower().startswith("c") else chain.puts
    if leg is None or leg.empty:
        return None
    row = leg.iloc[(leg["strike"] - strike_usd).abs().argmin()]
    last = float(row.get("lastPrice") or 0)
    return last if last > 0 else None


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
            out["adr_ratio"] = us_options_logic.US_ADR_MAP.get(us_code, {}).get("adr_ratio")
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
            last = _us_option_last(
                us_code, d.get("opt_type"), float(d.get("opt_strike") or 0),
                d.get("opt_expiry_iso"), out["current_fx"], out["adr_ratio"],
            )
            if last:
                out["opt_last_usd"] = last
                out["source"] = "market"
    except Exception:
        pass

    return jsonify(out)


@app.route("/get_custom_stocks")
@require_auth
def get_custom_stocks():
    return jsonify(db.get_custom_stocks(g.user["id"]))


@app.route("/save_custom_stocks", methods=["POST"])
@require_auth
def save_custom_stocks():
    db.save_custom_stocks(g.user["id"], request.json or [])
    return jsonify({"ok": True})


@app.route("/fetch", methods=["POST"])
@require_auth
def fetch():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    df, error, meta = warrant_logic.fetch_warrants(
        stock_codes,
        option_type,
        min_days,
        max_days,
        min_leverage,
        max_tv_pct,
        min_volume,
    )
    if error and df.empty:
        return jsonify({"rows": [], "count": 0, "error": error, **meta})
    return jsonify({"rows": df.to_dict(orient="records"), "count": len(df), **meta})


@app.route("/download", methods=["POST"])
@require_auth
def download():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    df, error, _meta = warrant_logic.fetch_warrants(
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


@app.route("/fetch_options", methods=["POST"])
@require_auth
def fetch_options():
    data = request.json
    stock_codes = data.get("stock_codes", ["TXO"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    min_volume = int(data.get("min_volume", 0))
    try:
        df = options_logic.fetch_options(
            stock_codes, option_type, min_days, max_days, min_leverage, min_volume
        )
        # Use pandas JSON serialisation so NaN → null (browser JSON.parse rejects bare NaN)
        rows = json.loads(df.to_json(orient="records"))
        return jsonify({"rows": rows, "count": len(df), "as_of": _epoch_iso(options_logic.data_as_of(stock_codes))})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


def _us_options_scan(stock_codes, option_type, min_days, max_days, min_volume):
    """American (US ADR) option chains for the scanner, in native USD.

    Wraps fetch_us_options (which also carries the TWD-normalized view used by
    the arb finder) and returns the USD-native columns a US options trader
    expects: strike_usd, bid_usd, ask_usd, adr_price (underlying), plus IV /
    delta / volume / OI.
    """
    frames, errors = [], []
    for code in stock_codes:
        try:
            df = us_options_logic.fetch_us_options(
                code, option_type, min_days=max(1, int(min_days)),
                max_days=int(max_days), compute_iv=True,
            )
            if int(min_volume) > 0:
                df = df[df["volume"] >= int(min_volume)]
            if not df.empty:
                df = df.assign(product=code)
                frames.append(df)
        except Exception as e:
            errors.append(f"{code}: {e}")
    if not frames:
        raise RuntimeError("; ".join(errors) if errors else "No data returned")
    out = pd.concat(frames, ignore_index=True)
    cols = ["product", "contract", "type", "strike_usd", "adr_price",
            "days_to_expiry", "bid_usd", "ask_usd", "iv_ask", "iv_bid",
            "delta_calc", "volume", "oi", "fx", "is_live"]
    return out[[c for c in cols if c in out.columns]]


@app.route("/us_options", methods=["POST"])
@require_auth
def us_options():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    min_days = data.get("min_days", 0)
    max_days = data.get("max_days", 365)
    min_volume = data.get("min_volume", 0)
    try:
        df = _us_options_scan(stock_codes, option_type, min_days, max_days, min_volume)
        rows = json.loads(df.to_json(orient="records"))
        as_of = min(
            (t for t in (us_options_logic.data_as_of(c) for c in stock_codes) if t),
            default=None,
        )
        return jsonify({"rows": rows, "count": len(df), "as_of": _epoch_iso(as_of)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/download_options", methods=["POST"])
@require_auth
def download_options():
    data = request.json
    stock_codes = data.get("stock_codes", ["TXO"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    min_volume = int(data.get("min_volume", 0))
    try:
        df = options_logic.fetch_options(
            stock_codes, option_type, min_days, max_days, min_leverage, min_volume
        )
    except Exception:
        df = pd.DataFrame()
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
    try:
        df = options_logic.fetch_options(stock_codes, option_type, min_days=1)
    except Exception as e:
        return jsonify({"error": str(e)})

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


def _match_warrants_to_options(warrant_df, opt_df, opt_contract_size,
                               max_strike_diff_pct, max_dte_diff,
                               positive_loose=False):
    rows = []
    seen = set()  # deduplicate (warrant_code, option_contract) pairs

    for direction in ("positive", "negative"):
        for _, w in warrant_df.iterrows():
            candidates = opt_df[opt_df["type"] == w["type"]].copy()
            if candidates.empty:
                continue

            candidates["strike_diff_pct"] = (
                (candidates["strike"] - w["strike"]).abs() / w["strike"] * 100
            )
            candidates["dte_diff"] = (
                (candidates["days_to_expiry"] - w["days_to_expiry"]).abs()
            )

            # Positive: opt_strike >= warrant_strike (buy warrant, sell option)
            # Negative: opt_strike <= warrant_strike (buy option, sell warrant)
            strike_filter = (
                candidates["strike"] >= w["strike"]
                if direction == "positive"
                else candidates["strike"] <= w["strike"]
            )

            # Never hold the SHORT leg as the longer-dated one — the long
            # (hedge) leg would expire first, leaving a naked short position.
            #   Positive: short = option -> require opt_dte <= warrant_dte.
            #   Negative: short = warrant -> require warrant_dte <= opt_dte,
            #     with max_dte_diff bounding the remaining (safe) gap.
            # The safe side (long leg outliving the short) is otherwise fine.
            if direction == "positive":
                dte_ok = candidates["days_to_expiry"] <= w["days_to_expiry"]
            else:
                bad_dte = (w["days_to_expiry"] - candidates["days_to_expiry"]).clip(lower=0)
                dte_ok = bad_dte <= max_dte_diff

            candidates = candidates[
                (candidates["strike_diff_pct"] <= max_strike_diff_pct)
                & dte_ok
                & strike_filter
            ]
            if candidates.empty:
                continue

            ratio = float(w["exercise_ratio"])
            if ratio <= 0:
                continue  # can't size or normalise a warrant with no exercise ratio
            warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
            warrant_bid_val = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else float(w["ask"])
            warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
            warrants_needed = round(opt_contract_size / ratio)
            is_put = w["type"] == "Put"
            # IV per leg (matched pair only, cheap) so the modal can mark an OTM
            # surviving leg at its time value ("sell") — the fetch dropped IV.
            if pd.notna(w.get("iv_ask")):
                warrant_iv = round(float(w["iv_ask"]), 4)
            else:
                _wiv = warrant_logic.implied_vol(
                    float(w["ask"]), float(w["underlying_price"]), float(w["strike"]),
                    int(w["days_to_expiry"]) / 365.0, 0.02, ratio, is_put)
                warrant_iv = round(float(_wiv), 4) if pd.notna(_wiv) and 0 < float(_wiv) <= 3 else None
            warrant_bid_disp = round(float(w["bid"]), 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else None

            # Emit EVERY profitable option for this warrant, not just the
            # strike/DTE-closest one — a farther-but-profitable pair must not be
            # hidden behind a closer-but-unprofitable "best".
            for _, opt in candidates.iterrows():
                if pd.isna(opt.get("ask")) or float(opt["ask"]) <= 0:
                    continue
                opt_bid_per_share = round(float(opt["bid"]), 4) if pd.notna(opt.get("bid")) and float(opt.get("bid", 0)) > 0 else None
                opt_ask_per_share = round(float(opt["ask"]), 4)

                # Positive tight (executable): opt_bid - warrant_ask > 0
                # Positive loose: opt_ask - warrant_bid > 0 (mirrors negative formula)
                # Negative (always loose): opt_bid - warrant_ask < 0
                if direction == "positive":
                    if positive_loose:
                        price_diff = round(opt_ask_per_share - warrant_bid_per_share, 4)
                        exec_opt = opt_ask_per_share
                        exec_warrant = warrant_bid_per_share
                    else:
                        if opt_bid_per_share is None:
                            continue  # tight positive sells the option at its bid
                        price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                        exec_opt = opt_bid_per_share
                        exec_warrant = warrant_ask_per_share
                    if price_diff <= 0:
                        continue
                else:
                    if opt_bid_per_share is None:
                        continue
                    price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                    exec_opt = opt_bid_per_share
                    exec_warrant = warrant_ask_per_share
                    if price_diff >= 0:
                        continue

                pair_key = (w["warrant_code"], opt["contract"])
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                price_diff_pct = round(price_diff / exec_opt * 100, 2) if exec_opt > 0 else None

                if direction == "positive":
                    trade = "Buy Warrant / Sell Option"
                else:
                    trade = "Buy Option / Sell Warrant"

                if pd.notna(opt.get("iv_bid")):
                    opt_iv = round(float(opt["iv_bid"]), 4)
                else:
                    _omid = None
                    if pd.notna(opt.get("bid")) and pd.notna(opt.get("ask")) and float(opt["bid"]) > 0 and float(opt["ask"]) > 0:
                        _omid = (float(opt["bid"]) + float(opt["ask"])) / 2
                    elif pd.notna(opt.get("ask")) and float(opt["ask"]) > 0:
                        _omid = float(opt["ask"])
                    _oiv = warrant_logic.implied_vol(
                        _omid, float(opt["underlying_price"]), float(opt["strike"]),
                        int(opt["days_to_expiry"]) / 365.0, options_logic.R, 1.0, is_put) if _omid else None
                    opt_iv = round(float(_oiv), 4) if (_oiv is not None and pd.notna(_oiv) and 0 < float(_oiv) <= 3) else None

                rows.append({
                    "warrant_code": w["warrant_code"],
                    "warrant_name": w["warrant_name"],
                    "option_contract": opt["contract"],
                    "type": w["type"],
                    "trade": trade,
                    "underlying_price": w["underlying_price"],
                    "warrant_dte": int(w["days_to_expiry"]),
                    "opt_dte": int(opt["days_to_expiry"]),
                    "dte_diff": int(opt["dte_diff"]),
                    "warrant_strike": w["strike"],
                    "opt_strike": round(float(opt["strike"]), 2),
                    "strike_diff_pct": round(float(opt["strike_diff_pct"]), 2),
                    "warrants_needed": warrants_needed,
                    "board_lots": round(warrants_needed / 1000, 4),
                    "opt_contract_size": opt_contract_size,
                    "warrant_ask": w["ask"],
                    "warrant_bid": warrant_bid_disp,
                    "opt_bid": opt_bid_per_share,
                    "opt_ask": opt_ask_per_share,
                    "warrant_per_share": exec_warrant,
                    "opt_per_share": exec_opt,
                    "price_diff": price_diff,
                    "price_diff_pct": price_diff_pct,
                    "warrant_iv": warrant_iv,
                    "opt_iv": opt_iv,
                    "iv_diff": round((opt_iv or 0) - (warrant_iv or 0), 4),
                })
    return rows


def _match_warrants_pcp(warrant_df, opt_df, opt_contract_size,
                        max_strike_diff_pct, max_dte_diff):
    """Put-Call-Parity matcher: price a warrant against the SYNTHETIC built from
    the OPPOSITE-type TAIFEX option plus the underlying and a risk-free bond.

    European PCP (no dividends): C - P = S - K*e^(-rT), so
        synthetic call = P + S - K*e^(-rT)
        synthetic put  = C - S + K*e^(-rT)
    using the OPTION's strike Ko, expiry T, and r = options_logic.R.

    Two directions per pair:
      - EXECUTABLE  (long warrant / short synthetic): buy warrant@ask, sell the
        option@bid, short the stock (call) / long the stock (put), lend/borrow
        PV(Ko). Kept only when the warrant is cheap vs synthetic (price_diff>0)
        AND the guards hold (short option expires no later than the long warrant,
        and the strike gap is on the no-downside side).
      - NON-EXECUTABLE (short warrant / long synthetic): warrants can't be
        shorted — emitted for debugging only, flagged executable=False, guards
        skipped. Kept when the warrant is rich vs synthetic (price_diff<0).
    """
    rows = []
    r = options_logic.R

    for _, w in warrant_df.iterrows():
        opp = "Put" if w["type"] == "Call" else "Call"
        candidates = opt_df[opt_df["type"] == opp].copy()
        if candidates.empty:
            continue

        ratio = float(w["exercise_ratio"])
        if ratio <= 0:
            continue  # can't size or normalise a warrant with no exercise ratio

        candidates["strike_diff_pct"] = (
            (candidates["strike"] - w["strike"]).abs() / w["strike"] * 100
        )
        candidates["dte_diff"] = (
            (candidates["days_to_expiry"] - w["days_to_expiry"]).abs()
        )
        candidates = candidates[candidates["strike_diff_pct"] <= max_strike_diff_pct]
        if candidates.empty:
            continue

        S = float(w["underlying_price"])
        Kw = float(w["strike"])
        is_call = w["type"] == "Call"
        warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
        warrant_bid_val = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else float(w["ask"])
        warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
        warrants_needed = round(opt_contract_size / ratio)
        warrant_bid_disp = round(float(w["bid"]), 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else None

        # Warrant-leg IV (display only; payoff is intrinsic). Use warrant type.
        if pd.notna(w.get("iv_ask")):
            warrant_iv = round(float(w["iv_ask"]), 4)
        else:
            _wiv = warrant_logic.implied_vol(
                float(w["ask"]), S, Kw,
                int(w["days_to_expiry"]) / 365.0, r, ratio, not is_call)
            warrant_iv = round(float(_wiv), 4) if pd.notna(_wiv) and 0 < float(_wiv) <= 3 else None

        for _, opt in candidates.iterrows():
            Ko = float(opt["strike"])
            To = int(opt["days_to_expiry"]) / 365.0
            bond_pv = round(Ko * float(np.exp(-r * To)), 4)
            opt_bid_ps = round(float(opt["bid"]), 4) if pd.notna(opt.get("bid")) and float(opt.get("bid", 0)) > 0 else None
            opt_ask_ps = round(float(opt["ask"]), 4) if pd.notna(opt.get("ask")) and float(opt.get("ask", 0)) > 0 else None

            # Option-leg IV (display only) — use the OPTION's put/call flag.
            is_put_opt = opp == "Put"
            if pd.notna(opt.get("iv_bid")):
                opt_iv = round(float(opt["iv_bid"]), 4)
            else:
                _omid = None
                if opt_bid_ps is not None and opt_ask_ps is not None:
                    _omid = (opt_bid_ps + opt_ask_ps) / 2
                elif opt_ask_ps is not None:
                    _omid = opt_ask_ps
                _oiv = warrant_logic.implied_vol(
                    _omid, S, Ko, To, r, 1.0, is_put_opt) if _omid else None
                opt_iv = round(float(_oiv), 4) if (_oiv is not None and pd.notna(_oiv) and 0 < float(_oiv) <= 3) else None

            def _synth(opt_ps):
                # synthetic call = P + S - PV(K);  synthetic put = C - S + PV(K)
                return round((opt_ps + S - bond_pv) if is_call else (opt_ps - S + bond_pv), 4)

            def _emit(executable, opt_ps, warrant_ps, price_diff):
                synthetic_price = _synth(opt_ps)
                pct = round(price_diff / synthetic_price * 100, 2) if synthetic_price > 0 else None
                if executable:
                    if is_call:
                        trade = "Buy Call Warrant / Short Synthetic (short Put + short stock + lend)"
                    else:
                        trade = "Buy Put Warrant / Short Synthetic (short Call + long stock + borrow)"
                else:
                    side = "short Put + short stock + lend" if is_call else "short Call + long stock + borrow"
                    trade = f"Short {w['type']} Warrant / Long Synthetic ({side}) — NON-EXECUTABLE"
                rows.append({
                    "warrant_code": w["warrant_code"],
                    "warrant_name": w["warrant_name"],
                    "option_contract": opt["contract"],
                    "type": w["type"],
                    "opt_type": opp,
                    "trade": trade,
                    "executable": bool(executable),
                    "underlying_price": round(S, 4),
                    "warrant_dte": int(w["days_to_expiry"]),
                    "opt_dte": int(opt["days_to_expiry"]),
                    "dte_diff": int(opt["dte_diff"]),
                    "warrant_strike": round(Kw, 2),
                    "opt_strike": round(Ko, 2),
                    "strike_diff_pct": round(float(opt["strike_diff_pct"]), 2),
                    "warrants_needed": warrants_needed,
                    "board_lots": round(warrants_needed / 1000, 4),
                    "opt_contract_size": opt_contract_size,
                    "warrant_ask": round(float(w["ask"]), 4),
                    "warrant_bid": warrant_bid_disp,
                    "opt_bid": opt_bid_ps,
                    "opt_ask": opt_ask_ps,
                    "warrant_per_share": warrant_ps,
                    "opt_per_share": round(opt_ps, 4),
                    "synthetic_price": synthetic_price,
                    "bond_pv": bond_pv,
                    "price_diff": round(price_diff, 4),
                    "price_diff_pct": pct,
                    "warrant_iv": warrant_iv,
                    "opt_iv": opt_iv,
                    "iv_diff": round((opt_iv or 0) - (warrant_iv or 0), 4),
                })

            # Executable: long warrant (buy@ask) vs short synthetic (sell opt@bid).
            if opt_bid_ps is not None:
                price_diff = round(_synth(opt_bid_ps) - warrant_ask_per_share, 4)
                # Guards: short option must not outlive the long warrant, and the
                # strike gap must be on the no-downside side (Call: Ko>=Kw, Put:
                # Ko<=Kw) so the residual is a bounded, never-negative vertical.
                dte_ok = int(opt["days_to_expiry"]) <= int(w["days_to_expiry"])
                strike_ok = (Ko >= Kw) if is_call else (Ko <= Kw)
                if price_diff > 0 and dte_ok and strike_ok:
                    _emit(True, opt_bid_ps, warrant_ask_per_share, price_diff)

            # Non-executable debug: short warrant (sell@bid) vs long synthetic
            # (buy opt@ask). No guards — warrants aren't shortable anyway.
            if opt_ask_ps is not None:
                price_diff = round(_synth(opt_ask_ps) - warrant_bid_per_share, 4)
                if price_diff < 0:
                    _emit(False, opt_ask_ps, warrant_bid_per_share, price_diff)

    return rows


def _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                  positive_loose=False, min_volume=0, strategy="same_type"):
    all_rows = []
    errors = []

    for code in stock_codes:
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no options data available")
            continue

        cfg = options_logic.COMMODITY_MAP[code]
        opt_contract_size = cfg["exercise_ratio"]

        # No time-value cap and no IV solve on the arb path: a positive price
        # arb only needs warrant ask + option bid, so nothing should drop a leg
        # over time value or a non-converging IV.
        warrant_df, err, _meta = warrant_logic.fetch_warrants(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            continue

        try:
            # PCP pairs a warrant with the OPPOSITE-type option, so the option
            # fetch must include both types regardless of the warrant filter.
            opt_type_fetch = "All" if strategy == "pcp" else option_type
            opt_df = options_logic.fetch_options([code], opt_type_fetch, min_days=1, compute_iv=False)
            opt_df = opt_df[opt_df["is_live"]]
            if min_volume > 0:
                opt_df = opt_df[opt_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        if opt_df.empty:
            errors.append(f"{code}: no live options")
            continue

        if strategy == "pcp":
            rows = _match_warrants_pcp(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
            )
        else:
            rows = _match_warrants_to_options(
                warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose,
            )
        all_rows.extend(rows)

    if not all_rows:
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result


R_FREE = 0.01875


def _build_us_match_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                       positive_loose=False, min_volume=0):
    """Direct-match Taiwan warrants against the same-underlying US ADR options.

    Reuses _match_warrants_to_options. The US option leg is pre-converted to
    TWD-per-Taiwan-share by us_options_logic, so it lives in the same price
    space as the warrant leg. One US contract controls 100 * adr_ratio Taiwan
    shares (per listing), which drives warrants_needed = contract_size/ratio.
    """
    all_rows = []
    errors = []

    for code in stock_codes:
        if code not in us_options_logic.US_ADR_MAP:
            errors.append(f"{code}: no US ADR mapping")
            continue

        contract_size = us_options_logic.contract_tw_shares(code)  # TW shares/contract

        # No time-value cap and no IV solve on the arb path (see _build_arb_df).
        warrant_df, err, _meta = warrant_logic.fetch_warrants(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            continue

        try:
            opt_df = us_options_logic.fetch_us_options(code, option_type, min_days=1, compute_iv=False)
            opt_df = opt_df[opt_df["is_live"]]
            if min_volume > 0:
                opt_df = opt_df[opt_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        if opt_df.empty:
            errors.append(f"{code}: no live US options")
            continue

        rows = _match_warrants_to_options(
            warrant_df, opt_df, contract_size, max_strike_diff_pct, max_dte_diff,
            positive_loose=positive_loose,
        )
        for r in rows:
            r["us_stock_code"] = code  # so the modal can pull ADR-premium history
        all_rows.extend(rows)

    if not all_rows:
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result


def _lcm(a, b):
    from math import gcd
    return a * b // gcd(a, b)


def _match_option_legs(tw_df, us_df, tw_contract_shares, us_contract_shares,
                       max_strike_diff_pct, max_dte_diff, positive_loose=False):
    """Match a Taiwan listed option to the *nearest* US ADR option (same type,
    closest strike, then closest expiry) so the two legs share ~the same payoff
    and delta roughly cancels.

    The two legs are NOT 1:1 — the ADR trades at a premium and FX floats — so
    this is not a risk-free arb. The trade is: sell the richer leg, buy the
    cheaper, and hold the residual ADR-premium + FX basis. The entry credit
    (executable: sell@bid, buy@ask) is the headline; the true edge is the
    probability-weighted P&L over where the premium lands by expiry, computed in
    the modal from the historical premium distribution (same engine as the
    US Option Match tab). The TW leg fills the modal's ``warrant_*`` slots, the
    US leg the ``opt_*`` slots.
    """
    base = _lcm(int(tw_contract_shares), int(us_contract_shares))
    tw_contracts = base // int(tw_contract_shares)
    us_contracts = base // int(us_contract_shares)
    matched_shares = base

    rows = []
    for _, tw in tw_df.iterrows():
        cands = us_df[us_df["type"] == tw["type"]].copy()
        if cands.empty:
            continue

        cands["strike_diff_pct"] = (
            (cands["strike"] - tw["strike"]).abs() / tw["strike"] * 100
        )
        cands["dte_diff"] = (cands["days_to_expiry"] - tw["days_to_expiry"]).abs()
        cands = cands[
            (cands["strike_diff_pct"] <= max_strike_diff_pct)
            & (cands["dte_diff"] <= max_dte_diff)
        ]
        if cands.empty:
            continue

        # Best pair = closest strike, then closest expiry (delta-match priority).
        cands = cands.sort_values(["strike_diff_pct", "dte_diff"])
        us = cands.iloc[0]

        tw_bid = float(tw["bid"]) if pd.notna(tw.get("bid")) and float(tw.get("bid", 0)) > 0 else None
        tw_ask = float(tw["ask"]) if pd.notna(tw.get("ask")) and float(tw.get("ask", 0)) > 0 else None
        us_bid = float(us["bid"]) if pd.notna(us.get("bid")) and float(us.get("bid", 0)) > 0 else None
        us_ask = float(us["ask"]) if pd.notna(us.get("ask")) and float(us.get("ask", 0)) > 0 else None
        if None in (tw_bid, tw_ask, us_bid, us_ask):
            continue

        # Sell the richer leg (by mid), buy the cheaper. Executable prices.
        tw_mid = (tw_bid + tw_ask) / 2
        us_mid = (us_bid + us_ask) / 2
        if us_mid >= tw_mid:
            # US richer → Short US / Long TW: sell US@bid, buy TW@ask
            trade = "Long TW / Short US"
            exec_opt, exec_warrant = us_bid, tw_ask   # opt slot = US, warrant slot = TW
        else:
            # TW richer → Short TW / Long US: sell TW@bid, buy US@ask
            trade = "Long US / Short TW"
            exec_opt, exec_warrant = us_ask, tw_bid

        # The short leg must expire no later than the long leg. If the short
        # leg expired first, the long leg would be gone while the short lives
        # on — a naked short option, which is exactly the risk we refuse to
        # carry. Short = US when "Long TW / Short US", else TW.
        short_dte = int(us["days_to_expiry"]) if trade == "Long TW / Short US" else int(tw["days_to_expiry"])
        long_dte = int(tw["days_to_expiry"]) if trade == "Long TW / Short US" else int(us["days_to_expiry"])
        if short_dte > long_dte:
            continue  # would leave a naked short leg after the long expires

        # Strike must be on the FAVORABLE side or the pair is just a vertical
        # spread with a real max loss, not a no-downside structure. With a
        # received credit the no-loss vertical requires:
        #   Call: short strike >= long strike (short the higher call)
        #   Put:  short strike <= long strike (short the lower put)
        # Anything else has min payoff = credit − (unfavorable gap) < 0.
        short_strike = float(us["strike"]) if trade == "Long TW / Short US" else float(tw["strike"])
        long_strike = float(tw["strike"]) if trade == "Long TW / Short US" else float(us["strike"])
        if tw["type"] == "Put":
            if short_strike > long_strike:
                continue  # short the higher put -> downside loss
        else:
            if short_strike < long_strike:
                continue  # short the lower call -> downside loss

        # Entry credit per share (sell price − buy price), sign per direction.
        # pcp_diff sign drives the modal payoff direction: >0 long-TW/short-US.
        if trade == "Long TW / Short US":
            credit = round(exec_opt - exec_warrant, 4)     # us_bid − tw_ask
        else:
            credit = round(exec_warrant - exec_opt, 4)     # tw_bid − us_ask
        if credit <= 0:
            continue  # no executable entry credit
        pcp_diff = credit if trade == "Long TW / Short US" else -credit

        # IV for each leg (matched pairs only, so cheap) — the modal needs it to
        # mark the not-yet-expired leg with time value at the trade horizon, so
        # the scenario P&L varies with the premium instead of being flat.
        is_put = tw["type"] == "Put"
        tw_iv = warrant_logic.implied_vol(
            tw_mid, float(tw["underlying_price"]), float(tw["strike"]),
            int(tw["days_to_expiry"]) / 365.0, options_logic.R, 1.0, is_put)
        us_iv = warrant_logic.implied_vol(
            us_mid, float(us["underlying_price"]), float(us["strike"]),
            int(us["days_to_expiry"]) / 365.0, us_options_logic.R_US, 1.0, is_put)
        tw_iv = round(float(tw_iv), 4) if pd.notna(tw_iv) and 0 < float(tw_iv) <= 3 else None
        us_iv = round(float(us_iv), 4) if pd.notna(us_iv) and 0 < float(us_iv) <= 3 else None

        denom = exec_opt if exec_opt else 1
        rows.append({
            "warrant_code": tw["contract"],
            "warrant_name": f"TW {tw['contract']}",
            "option_contract": us["contract"],
            "type": tw["type"],
            "warrant_type": tw["type"],
            "opt_type": us["type"],
            "trade": trade,
            "underlying_price": round(float(tw["underlying_price"]), 4),
            "warrant_dte": int(tw["days_to_expiry"]),
            "opt_dte": int(us["days_to_expiry"]),
            "dte_diff": int(us["dte_diff"]),
            "warrant_strike": round(float(tw["strike"]), 2),
            "opt_strike": round(float(us["strike"]), 2),
            "strike_diff_pct": round(float(us["strike_diff_pct"]), 2),
            "tw_contracts": int(tw_contracts),
            "us_contracts": int(us_contracts),
            "matched_shares": int(matched_shares),
            "warrants_needed": int(matched_shares),
            "opt_contract_size": int(matched_shares),
            "warrant_ask": round(tw_ask, 4),
            "warrant_bid": round(tw_bid, 4),
            "opt_bid": round(us_bid, 4),
            "opt_ask": round(us_ask, 4),
            "warrant_per_share": round(exec_warrant, 4),
            "opt_per_share": round(exec_opt, 4),
            "price_diff": pcp_diff,
            "price_diff_pct": round(credit / denom * 100, 2),
            "entry_credit": round(credit * matched_shares, 0),
            "warrant_iv": tw_iv,
            "opt_iv": us_iv,
            "iv_diff": round((us_iv or 0) - (tw_iv or 0), 4),
        })
    return rows


def _build_tw_us_option_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                           positive_loose=False, min_volume=0):
    """Match Taiwan listed options against US ADR options on the same underlying."""
    all_rows = []
    errors = []

    for code in stock_codes:
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no Taiwan options")
            continue
        if code not in us_options_logic.US_ADR_MAP:
            errors.append(f"{code}: no US ADR options")
            continue

        tw_contract_shares = options_logic.COMMODITY_MAP[code]["exercise_ratio"]  # 2000
        us_contract_shares = us_options_logic.contract_tw_shares(code)            # 500 (2303)

        try:
            # Like US Option Match's warrant leg: don't require a live two-sided
            # quote on the TW option leg — fall back to the last settlement
            # snapshot so the scan still works when TAIFEX is closed. (Off-hours
            # prices are stale marks, not executable until the market reopens.)
            tw_df = options_logic.fetch_options([code], option_type, min_days=1, compute_iv=False)
            if min_volume > 0:
                tw_df = tw_df[tw_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: TW options {e}")
            continue

        try:
            us_df = us_options_logic.fetch_us_options(code, option_type, min_days=1, compute_iv=False)
            us_df = us_df[us_df["is_live"]]
            if min_volume > 0:
                us_df = us_df[us_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: US options {e}")
            continue

        if tw_df.empty or us_df.empty:
            errors.append(f"{code}: no live options on one leg")
            continue

        rows = _match_option_legs(
            tw_df, us_df, tw_contract_shares, us_contract_shares,
            max_strike_diff_pct, max_dte_diff, positive_loose=positive_loose,
        )
        for r in rows:
            r["us_stock_code"] = code
        all_rows.extend(rows)

    if not all_rows:
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result


@app.route("/adr_premium", methods=["POST"])
@require_auth
def adr_premium():
    data = request.json
    stock_code = data.get("stock_code")
    try:
        if stock_code not in us_options_logic.US_ADR_MAP:
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
        if stock_code not in us_options_logic.US_ADR_MAP:
            raise RuntimeError(f"{stock_code}: no US ADR mapping")
        return jsonify(us_options_logic.adr_premium_scenario(stock_code, horizon_days))
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/us_option_match", methods=["POST"])
@require_auth
def us_option_match():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = _build_us_match_df(stock_codes, option_type, max_strike_diff_pct,
                                max_dte_diff, positive_loose=positive_loose,
                                min_volume=min_volume)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/us_option_match_csv", methods=["POST"])
@require_auth
def us_option_match_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = _build_us_match_df(stock_codes, option_type, max_strike_diff_pct,
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
        headers={"Content-Disposition": "attachment; filename=us_option_match.csv"},
    )


@app.route("/tw_us_option_match", methods=["POST"])
@require_auth
def tw_us_option_match():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = _build_tw_us_option_df(stock_codes, option_type, max_strike_diff_pct,
                                    max_dte_diff, positive_loose=positive_loose,
                                    min_volume=min_volume)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/tw_us_option_match_csv", methods=["POST"])
@require_auth
def tw_us_option_match_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    try:
        df = _build_tw_us_option_df(stock_codes, option_type, max_strike_diff_pct,
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


@app.route("/arb_finder", methods=["POST"])
@require_auth
def arb_finder():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                           positive_loose=positive_loose, min_volume=min_volume,
                           strategy=strategy)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        as_of = min(
            (t for t in (warrant_logic.cache_as_of(stock_codes),
                         options_logic.data_as_of(stock_codes)) if t),
            default=None,
        )
        return jsonify({"rows": rows, "count": len(rows), "as_of": _epoch_iso(as_of)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/arb_finder_csv", methods=["POST"])
@require_auth
def arb_finder_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
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


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/refresh", methods=["POST"])
@require_auth
def refresh():
    kind = (request.json or {}).get("kind", "all")
    return jsonify(scheduler.force_refresh(kind))


if __name__ == "__main__":
    # Local dev entry point. Production runs via wsgi.py + gunicorn.
    scheduler.start()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
