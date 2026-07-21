import sys
import io

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from flask import Flask, render_template, request, jsonify, Response
import threading
import webbrowser
import warrant_logic
import options_logic
import us_options_logic
import os
import json
import socket
import signal
import subprocess
import time
import numpy as np
import pandas as pd
from scipy.interpolate import griddata

base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# User data (portfolio, custom stocks) must persist across runs. When frozen,
# base_dir is a per-launch temp extraction dir (_MEIPASS) that is wiped each
# run — writing data there loses it. Keep data next to the executable instead
# (in dev this is the source dir). Templates/static still load from base_dir.
if getattr(sys, "frozen", False):
    data_dir = os.path.dirname(sys.executable)
else:
    data_dir = os.path.dirname(os.path.abspath(__file__))
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

CUSTOM_STOCKS_FILE = os.path.join(data_dir, "custom_stocks.json")
PORTFOLIO_FILE = os.path.join(data_dir, "portfolio.json")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/get_portfolio")
def get_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return jsonify(json.load(f))
    return jsonify([])


@app.route("/save_portfolio", methods=["POST"])
def save_portfolio():
    trades = request.json
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)
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
def get_custom_stocks():
    if os.path.exists(CUSTOM_STOCKS_FILE):
        with open(CUSTOM_STOCKS_FILE) as f:
            return jsonify(json.load(f))
    return jsonify([])


@app.route("/save_custom_stocks", methods=["POST"])
def save_custom_stocks():
    stocks = request.json
    with open(CUSTOM_STOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False)
    return jsonify({"ok": True})


@app.route("/fetch", methods=["POST"])
def fetch():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    df, error = warrant_logic.fetch_warrants(
        stock_codes,
        option_type,
        min_days,
        max_days,
        min_leverage,
        max_tv_pct,
        min_volume,
    )
    if error and df.empty:
        return jsonify({"rows": [], "count": 0, "error": error})
    return jsonify({"rows": df.to_dict(orient="records"), "count": len(df)})


@app.route("/download", methods=["POST"])
def download():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    min_days = int(data.get("min_days", 0))
    max_days = int(data.get("max_days", 365))
    min_leverage = float(data.get("min_leverage", 0.0))
    max_tv_pct = float(data.get("max_tv_pct", 100.0))
    min_volume = int(data.get("min_volume", 0))

    df, error = warrant_logic.fetch_warrants(
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
        return jsonify({"rows": rows, "count": len(df)})
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
        return jsonify({"rows": rows, "count": len(df)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/download_options", methods=["POST"])
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
def iv_surface():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    highlight_code = data.get("highlight_code", "").strip()

    df_clean, error = warrant_logic.fetch_iv_surface(stock_codes, option_type)
    if df_clean is None:
        return jsonify({"error": error})

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

            # The residual after pairing two same-type contracts is a vertical
            # spread, and it must be the FAVORABLE one — payoff >= 0 at every
            # spot — so the entry credit is never clawed back at expiry.
            #
            # Long the K_long leg, short the K_short leg:
            #   calls: favorable iff K_short >= K_long (bull call spread)
            #   puts : favorable iff K_short <= K_long (bear put spread)
            # Puts invert because a put pays off downward: the leg you are short
            # must be the one that starts paying LAST as spot falls.
            #
            # Positive = buy warrant / sell option -> K_opt vs K_warrant.
            # Negative = buy option / sell warrant -> the comparison flips.
            is_put_w = w["type"] == "Put"
            long_side_is_warrant = direction == "positive"
            opt_ge_warrant = long_side_is_warrant != is_put_w
            favorable = (
                candidates["strike"] >= w["strike"]
                if opt_ge_warrant
                else candidates["strike"] <= w["strike"]
            )
            candidates["favorable"] = favorable
            # Max loss per share of the residual vertical at expiry: zero on the
            # favorable side (payoff >= 0 everywhere), |Kw - Ko| on the other.
            candidates["max_loss_per_share"] = np.where(
                favorable, 0.0, (candidates["strike"] - w["strike"]).abs()
            )
            # max_strike_diff_pct bounds that MAX LOSS — it is not a similarity
            # metric. On the favorable side the loss is already zero no matter
            # how far the strike sits, so distance there is irrelevant and the
            # cap must not apply: a deep-OTM option whose per-share price still
            # beats the warrant is a riskless arb. Only the unfavorable side,
            # where the spread can be clawed back at expiry, needs the cap.
            # (The far favorable side is self-policing anyway — as Ko runs away
            # from Kw the option gets cheaper per share than the warrant and the
            # price test below rejects it.)
            strike_ok = favorable | (candidates["strike_diff_pct"] <= max_strike_diff_pct)

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

            candidates = candidates[strike_ok & dte_ok]
            if candidates.empty:
                continue

            ratio = float(w["exercise_ratio"])
            if ratio <= 0:
                continue  # can't size or normalise a warrant with no exercise ratio
            warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
            warrant_bid_live = pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0
            # Loose marks tolerate the ask-as-bid fallback; an EXECUTABLE sell
            # does not — nobody lifts a bid that isn't there. Keep both, and let
            # the tight path below demand the real one.
            warrant_bid_val = float(w["bid"]) if warrant_bid_live else float(w["ask"])
            warrant_bid_per_share = round(warrant_bid_val / ratio, 4)
            warrant_bid_real_per_share = round(float(w["bid"]) / ratio, 4) if warrant_bid_live else None
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
                # options_logic backfills the MISSING side of a one-sided quote
                # with settlement (or last), so opt["bid"]/opt["ask"] are always
                # populated and cannot be trusted on their own. The *_live flags
                # record which side was a genuine quote; only a genuine quote is
                # an executable price, so null out the backfilled side here and
                # let each formula below require the side it actually trades.
                opt_ask_live = bool(opt.get("ask_live", True)) and pd.notna(opt.get("ask")) and float(opt.get("ask") or 0) > 0
                opt_bid_live = bool(opt.get("bid_live", True)) and pd.notna(opt.get("bid")) and float(opt.get("bid") or 0) > 0
                if not (opt_ask_live or opt_bid_live):
                    continue
                opt_bid_per_share = round(float(opt["bid"]), 4) if opt_bid_live else None
                opt_ask_per_share = round(float(opt["ask"]), 4) if opt_ask_live else None

                # Both directions now honour the loose/tight toggle symmetrically.
                # Tight = the prices you can actually hit: you LIFT the ask on
                # whatever you buy and HIT the bid on whatever you sell.
                # Loose = the optimistic mark (mid-ish), for ranking only.
                #
                #   positive (buy warrant / sell option)
                #     tight: opt_bid  - warrant_ask  > 0
                #     loose: opt_ask  - warrant_bid  > 0
                #   negative (buy option / sell warrant)
                #     tight: opt_ask  - warrant_bid  < 0
                #     loose: opt_bid  - warrant_ask  < 0
                #
                # Negative previously used the loose formula unconditionally,
                # which priced a BUY at the bid and a SELL at the ask — both
                # unattainable, so every negative row overstated its edge.
                if direction == "positive":
                    if positive_loose:
                        if opt_ask_per_share is None:
                            continue  # loose positive marks against the option ask
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
                    if positive_loose:
                        if opt_bid_per_share is None:
                            continue  # loose negative marks against the option bid
                        price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)
                        exec_opt = opt_bid_per_share
                        exec_warrant = warrant_ask_per_share
                    else:
                        # Executable: buy the option at its ask, sell the warrant
                        # into its bid. A synthetic bid would fake the proceeds.
                        if opt_ask_per_share is None or warrant_bid_real_per_share is None:
                            continue
                        price_diff = round(opt_ask_per_share - warrant_bid_real_per_share, 4)
                        exec_opt = opt_ask_per_share
                        exec_warrant = warrant_bid_real_per_share
                    if price_diff >= 0:
                        continue

                pair_key = (w["warrant_code"], opt["contract"])
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                price_diff_pct = round(price_diff / exec_opt * 100, 2) if exec_opt > 0 else None

                if direction == "positive":
                    trade = "Buy Warrant / Sell Option"
                    # Buying the warrant lifts the ask -> resting SELL size gates it.
                    warrant_depth_lots = int(w.get("ask_qty") or 0)
                else:
                    trade = "Buy Option / Sell Warrant"
                    # Selling the warrant hits the bid -> resting BUY size gates it.
                    warrant_depth_lots = int(w.get("bid_qty") or 0)
                board_lots_needed = round(warrants_needed / 1000, 4)
                fillable = warrant_depth_lots >= board_lots_needed

                if pd.notna(opt.get("iv_bid")):
                    opt_iv = round(float(opt["iv_bid"]), 4)
                else:
                    # Mark off the LIVE side(s) only — a settlement-backfilled
                    # side would put a stale price into the IV solve.
                    if opt_bid_per_share is not None and opt_ask_per_share is not None:
                        _omid = (opt_bid_per_share + opt_ask_per_share) / 2
                    else:
                        _omid = opt_ask_per_share if opt_ask_per_share is not None else opt_bid_per_share
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
                    # Favorable residual = the vertical pays >= 0 at every spot,
                    # so the entry credit is never clawed back: a true arb.
                    # Otherwise the credit is at risk up to max_loss_per_share
                    # (per underlying share, before exercise-ratio sizing).
                    "riskless": bool(opt["favorable"]),
                    "max_loss_per_share": round(float(opt["max_loss_per_share"]), 4),
                    "warrants_needed": warrants_needed,
                    "board_lots": board_lots_needed,
                    "warrant_depth_lots": warrant_depth_lots,
                    "fillable": bool(fillable),
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
                        max_strike_diff_pct, max_dte_diff,
                        positive_loose=False, r=None,
                        synthetic_underlying="warrant"):
    """Put-Call-Parity matcher: price a warrant against the SYNTHETIC built from
    the OPPOSITE-type option plus the underlying and a risk-free bond.

    European PCP (no dividends): C - P = S - K*e^(-rT), so
        synthetic call = P + S - K*e^(-rT)
        synthetic put  = C - S + K*e^(-rT)
    using the OPTION's strike Ko, expiry T, and discount rate ``r``
    (defaults to ``options_logic.R``).

    ``synthetic_underlying`` selects the spot S plugged into the synthetic:
      - "warrant": use the warrant's own underlying (single-market TAIFEX PCP —
        both legs share the same underlying).
      - "option":  use the OPTION row's underlying — the cross-market case, where
        the option is a US ADR converted to TWD/TW-share. The ADR-converted spot
        differs from the warrant's local spot by the ADR premium, which is the
        whole edge; the synthetic (put + ADR + USD bond) must be priced off the
        ADR, so pass ``r = us_options_logic.R_US`` too. The emitted row keeps the
        warrant's LOCAL underlying in ``underlying_price`` (premium baseline +
        warrant payoff chart) and carries the ADR spot in ``adr_underlying``.

    Two directions per pair:
      - EXECUTABLE  (long warrant / short synthetic): buy warrant@ask, sell the
        option@bid, short the stock (call) / long the stock (put), lend/borrow
        PV(Ko). Kept only when the warrant is cheap vs synthetic (price_diff>0)
        AND the guards hold (short option expires no later than the long warrant,
        and the strike gap is on the no-downside side). This is the ONLY tradable
        side (never shorts the warrant), so ``positive_loose`` applies here only:
        loose prices the two tradable quotes at their favorable side — buy
        warrant@bid, sell option@ask (spread not covered) — mirroring the Direct
        Match positive-loose leg. Stock/bond legs are theoretical and unchanged.
      - NON-EXECUTABLE (short warrant / long synthetic): warrants can't be
        shorted — emitted for debugging only, flagged executable=False, guards
        skipped. Kept when the warrant is rich vs synthetic (price_diff<0).
    """
    rows = []
    if r is None:
        r = options_logic.R
    s_from_option = synthetic_underlying == "option"

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

        S_local = float(w["underlying_price"])   # warrant's own (local) spot
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
                float(w["ask"]), S_local, Kw,
                int(w["days_to_expiry"]) / 365.0, r, ratio, not is_call)
            warrant_iv = round(float(_wiv), 4) if pd.notna(_wiv) and 0 < float(_wiv) <= 3 else None

        for _, opt in candidates.iterrows():
            # Spot for the synthetic: the option's underlying in the cross-market
            # case (ADR-converted), else the warrant's own local spot.
            S = float(opt["underlying_price"]) if s_from_option else S_local
            Ko = float(opt["strike"])
            To = int(opt["days_to_expiry"]) / 365.0
            bond_pv = round(Ko * float(np.exp(-r * To)), 4)
            # Same one-sided-quote handling as the Direct matcher: the missing
            # side arrives settlement-backfilled, so honour the *_live flags and
            # keep only genuinely quoted sides as executable prices.
            _oa_live = bool(opt.get("ask_live", True)) and pd.notna(opt.get("ask")) and float(opt.get("ask") or 0) > 0
            _ob_live = bool(opt.get("bid_live", True)) and pd.notna(opt.get("bid")) and float(opt.get("bid") or 0) > 0
            opt_bid_ps = round(float(opt["bid"]), 4) if _ob_live else None
            opt_ask_ps = round(float(opt["ask"]), 4) if _oa_live else None

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
                # Executable side buys the warrant (lifts ask -> SELL size gates);
                # the non-executable debug side would short it (hits bid).
                warrant_depth_lots = int(w.get("ask_qty") or 0) if executable else int(w.get("bid_qty") or 0)
                board_lots_needed = round(warrants_needed / 1000, 4)
                fillable = warrant_depth_lots >= board_lots_needed
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
                    "underlying_price": round(S_local, 4),
                    "adr_underlying": round(S, 4),
                    "warrant_dte": int(w["days_to_expiry"]),
                    "opt_dte": int(opt["days_to_expiry"]),
                    "dte_diff": int(opt["dte_diff"]),
                    "warrant_strike": round(Kw, 2),
                    "opt_strike": round(Ko, 2),
                    "strike_diff_pct": round(float(opt["strike_diff_pct"]), 2),
                    "warrants_needed": warrants_needed,
                    "board_lots": board_lots_needed,
                    "warrant_depth_lots": warrant_depth_lots,
                    "fillable": bool(fillable),
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

            # Executable: long warrant / short synthetic. Only tradable side, so
            # the loose toggle applies here (and here only).
            #   tight : buy warrant@ask, sell option@bid (real executable fills)
            #   loose : buy warrant@bid, sell option@ask (favorable side)
            exec_opt_ps = opt_ask_ps if positive_loose else opt_bid_ps
            exec_warrant_ps = warrant_bid_per_share if positive_loose else warrant_ask_per_share
            if exec_opt_ps is not None:
                price_diff = round(_synth(exec_opt_ps) - exec_warrant_ps, 4)
                # Guards: short option must not outlive the long warrant, and the
                # strike gap must be on the no-downside side (Call: Ko>=Kw, Put:
                # Ko<=Kw) so the residual is a bounded, never-negative vertical.
                dte_ok = int(opt["days_to_expiry"]) <= int(w["days_to_expiry"])
                strike_ok = (Ko >= Kw) if is_call else (Ko <= Kw)
                if price_diff > 0 and dte_ok and strike_ok:
                    _emit(True, exec_opt_ps, exec_warrant_ps, price_diff)

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
        warrant_df, err = warrant_logic.fetch_warrants(
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
            # A ONE-SIDED option quote is still a valid arb benchmark: each
            # direction only ever executes against one side of the option book
            # (positive tight sells the bid, positive loose / negative buys the
            # ask). Requiring is_live (both sides) threw away ~90% of the TW
            # option universe off-hours. The matcher gates each formula on the
            # side it actually needs, so keep anything with at least one side.
            opt_df = opt_df[opt_df["ask_live"] | opt_df["bid_live"]]
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
                positive_loose=positive_loose,
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
        if "riskless" in result.columns:
            # True arbs (residual vertical never clawed back at expiry) outrank
            # capped-loss pairs regardless of headline edge.
            result = result.sort_values(
                ["riskless", "price_diff_pct"], ascending=[False, False]
            )
        else:
            result = result.sort_values("price_diff_pct", ascending=False)
    return result


R_FREE = 0.01875


# ── Straddle arbitrage (volatility relative-value) ───────────────────────────
# A "straddle package" = one call leg + one put leg on the same underlying (their
# strikes may differ within ΔK — a strangle — so packages carry net delta). We
# LONG the cheapest-implied-vol package (legs from either warrant or option, all
# bought) and SHORT the dearest-implied-vol package (OPTION legs only, since
# Taiwan warrants can't be shorted). Edge = short_iv − long_iv, in vol points.
# The comparison is done in IMPLIED VOL, not price, to strip out the intrinsic
# |F−K| (strike) and √T (expiry) terms that dominate raw straddle prices.

def _straddle_legs(warrant_df, opt_df, loose=False, short_warrants=False):
    """Flatten warrants + options into a common leg record list.

    px_* are per-underlying-share (warrant quote ÷ exercise ratio; options are
    already per-share).

    ``loose`` (DEBUG) prices every leg on its *favorable* side — buy at the bid,
    write at the ask — instead of the executable side (buy ask / write bid). It
    does not cross the spread, so any edge it reports is not tradeable; it exists
    to confirm the scanner wires up at all when live quotes are thin. Mirrors the
    ``positive_loose`` toggle in the Direct / US arb finders.

    ``short_warrants`` (DEBUG) marks warrant legs shortable. Taiwan warrants can
    only be written by the issuing bank, so any row using a short warrant leg is
    NOT executable — this exists purely to enlarge the short-side candidate pool
    when the option chain has too few live quotes to form packages.
    """
    legs = []
    for _, w in warrant_df.iterrows():
        ratio = float(w["exercise_ratio"] or 0)
        ask = float(w["ask"] or 0)
        if ratio <= 0 or ask <= 0:
            continue
        bid = float(w["bid"]) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else 0.0

        def _wiv(col):
            v = w.get(col)
            return float(v) if pd.notna(v) and 0 < float(v) <= 3 else None

        iv_ask, iv_bid = _wiv("iv_ask"), _wiv("iv_bid")
        # Executable: buy at ask. Loose: buy at bid (favorable, not tradeable).
        buy_px, buy_iv = (bid, iv_bid) if loose else (ask, iv_ask)
        # Write side is the mirror: executable hits the bid, loose lifts the ask.
        sell_px, sell_iv = (ask, iv_ask) if loose else (bid, iv_bid)
        legs.append({
            "source": "warrant", "type": w["type"], "K": float(w["strike"]),
            "dte": int(w["days_to_expiry"]), "id": str(w["warrant_code"]),
            "name": w["warrant_name"], "ratio": ratio,
            "px_buy": round(buy_px / ratio, 4) if buy_px > 0 else None,
            "iv_buy": buy_iv,
            "px_sell": (round(sell_px / ratio, 4) if (short_warrants and sell_px > 0) else None),
            "iv_sell": sell_iv if short_warrants else None,
            "shortable": bool(short_warrants),
            "S": float(w["underlying_price"]), "vol": int(w.get("volume") or 0),
        })
    for _, o in opt_df.iterrows():
        ask = float(o["ask"] or 0) if pd.notna(o.get("ask")) else 0.0
        bid = float(o["bid"] or 0) if pd.notna(o.get("bid")) else 0.0
        S = float(o["underlying_price"])
        K = float(o["strike"])
        T = int(o["days_to_expiry"]) / 365.0
        is_put = o["type"] == "Put"
        # Compute option IV OURSELVES from the per-share quote (ratio=1.0) so it's
        # on the same scale as the warrant IV. options_logic's own fetch-IV mixes
        # per-share price with ratio=2000, which mis-scales it ~10x — never trust
        # it for a cross-instrument vol comparison (the Direct arb recomputes too).
        def _oiv(px):
            if px <= 0:
                return None
            v = warrant_logic.implied_vol(px, S, K, T, R_FREE, 1.0, is_put)
            return round(float(v), 4) if pd.notna(v) and 0 < float(v) <= 3 else None

        buy_px = bid if loose else ask
        sell_px = ask if loose else bid
        legs.append({
            "source": "option", "type": o["type"], "K": K,
            "dte": int(o["days_to_expiry"]), "id": str(o["contract"]),
            "name": o["contract"], "ratio": float(o.get("exercise_ratio") or 2000),
            "px_buy": round(buy_px, 4) if buy_px > 0 else None, "iv_buy": _oiv(buy_px),
            "px_sell": round(sell_px, 4) if sell_px > 0 else None, "iv_sell": _oiv(sell_px),
            "shortable": True,
            "S": S, "vol": int(o.get("volume") or 0),
        })
    return legs


def _collapse_best(legs, typ, side):
    """Keep the single best leg per (strike, dte) for one side.

    side="buy": legs with px_buy+iv_buy, keep MIN iv_buy (cheapest vol to buy).
    side="sell": shortable legs with px_sell+iv_sell, keep MAX iv_sell (dearest
    vol to write). Collapses many same-strike issuer warrants to the best one.
    """
    best = {}
    for lg in legs:
        if lg["type"] != typ:
            continue
        if side == "buy":
            if lg["px_buy"] is None or lg["iv_buy"] is None:
                continue
            iv = lg["iv_buy"]
        else:
            if not lg["shortable"] or lg["px_sell"] is None or lg["iv_sell"] is None:
                continue
            iv = lg["iv_sell"]
        key = (round(lg["K"], 2), lg["dte"])
        cur = best.get(key)
        if cur is None or (side == "buy" and iv < cur[0]) or (side == "sell" and iv > cur[0]):
            best[key] = (iv, lg)
    return [v[1] for v in best.values()]


def _straddles(calls, puts, side, S, max_k_pct, max_dte_diff):
    """Form call+put packages within ΔK/ΔDTE. Returns list of straddle dicts."""
    out = []
    for c in calls:
        for p in puts:
            if abs(c["K"] - p["K"]) / S * 100 > max_k_pct:
                continue
            if abs(c["dte"] - p["dte"]) > max_dte_diff:
                continue
            if side == "buy":
                iv = (c["iv_buy"] + p["iv_buy"]) / 2
                px = c["px_buy"] + p["px_buy"]
            else:
                iv = (c["iv_sell"] + p["iv_sell"]) / 2
                px = c["px_sell"] + p["px_sell"]
            out.append({
                "call": c, "put": p, "iv": iv, "px": px,
                "K": (c["K"] + p["K"]) / 2, "dte": min(c["dte"], p["dte"]),
            })
    return out


def _build_straddle_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                       min_volume=0, min_iv_edge=1.0, loose=False,
                       short_warrants=False, require_dte_cover=True):
    """Straddle vol-arb rows: LONG cheap package vs SHORT dear option package.

    option_type is accepted for API symmetry but ignored — a straddle needs both
    calls and puts, so both are always fetched.

    ``loose`` / ``short_warrants`` are DEBUG relaxations — see _straddle_legs.
    Rows produced under either flag are not executable.

    ``require_dte_cover`` enforces that every short leg expires on or before
    every long leg, so the covering package is still alive at short expiry.
    Without it the scanner emits rows that leave a naked short — the same
    failure the Direct arb guards against with ``opt_dte <= warrant_dte``.
    """
    all_rows = []
    errors = []
    # Stage counters so a zero-row scan can explain WHICH filter emptied it
    # rather than failing with a bare "no matches".
    diag = {"legs": 0, "longs": 0, "shorts": 0, "pairs": 0,
            "cut_dte_cover": 0, "cut_edge": 0, "best_edge": None}
    CONTRACT = 2000  # one TAIFEX single-stock option = 2000 shares

    for code in stock_codes:
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no options data available")
            continue

        warrant_df, werr = warrant_logic.fetch_warrants(
            [code], "All", 0, 365, 0, 1e9, 0, compute_iv=True
        )
        if warrant_df.empty:
            errors.append(f"{code}: {werr or 'no warrants'}")
            continue
        try:
            opt_df = options_logic.fetch_options([code], "All", min_days=1, compute_iv=True)
            opt_df = opt_df[opt_df["is_live"]]
            if min_volume > 0:
                opt_df = opt_df[opt_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue
        if opt_df.empty:
            errors.append(f"{code}: no live options")
            continue

        legs = _straddle_legs(warrant_df, opt_df, loose=loose,
                              short_warrants=short_warrants)
        if not legs:
            continue
        diag["legs"] += len(legs)
        S = float(warrant_df["underlying_price"].iloc[0])

        buy_calls = _collapse_best(legs, "Call", "buy")
        buy_puts = _collapse_best(legs, "Put", "buy")
        sell_calls = _collapse_best(legs, "Call", "sell")
        sell_puts = _collapse_best(legs, "Put", "sell")
        if not (buy_calls and buy_puts and sell_calls and sell_puts):
            continue

        longs = _straddles(buy_calls, buy_puts, "buy", S, max_strike_diff_pct, max_dte_diff)
        shorts = _straddles(sell_calls, sell_puts, "sell", S, max_strike_diff_pct, max_dte_diff)
        diag["longs"] += len(longs)
        diag["shorts"] += len(shorts)
        if not (longs and shorts):
            continue

        seen = set()
        for sh in shorts:
            # Best long counterpart = lowest long_iv within K/DTE tolerance of
            # this short package (maximises the vol edge).
            short_ids = {sh["call"]["id"], sh["put"]["id"]}
            best_long = None
            for lo in longs:
                if abs(lo["K"] - sh["K"]) / S * 100 > max_strike_diff_pct:
                    continue
                if abs(lo["dte"] - sh["dte"]) > max_dte_diff:
                    continue
                # Never buy and write the SAME contract — that just crosses its
                # own bid-ask spread and shows a phantom edge.
                if {lo["call"]["id"], lo["put"]["id"]} & short_ids:
                    continue
                # Cover is PER OPTION TYPE, not across the package: a call can
                # only be covered by a call, a put only by a put. Assignment on
                # a short call means delivering shares (source them by
                # exercising the long call); assignment on a short put means
                # buying shares (offload them via the long put). A long call
                # cannot cover a short put — the put is assigned precisely in
                # the down-scenario where the call is worthless. So require the
                # long leg of each type to outlive the short leg of that type,
                # rather than the stricter min(long) >= max(short).
                if require_dte_cover and (
                    sh["call"]["dte"] > lo["call"]["dte"]
                    or sh["put"]["dte"] > lo["put"]["dte"]
                ):
                    diag["cut_dte_cover"] += 1
                    continue
                if best_long is None or lo["iv"] < best_long["iv"]:
                    best_long = lo
            if best_long is None:
                continue
            diag["pairs"] += 1
            edge_pts = (sh["iv"] - best_long["iv"]) * 100
            if diag["best_edge"] is None or edge_pts > diag["best_edge"]:
                diag["best_edge"] = edge_pts
            if edge_pts < min_iv_edge:
                diag["cut_edge"] += 1
                continue

            lc, lp = best_long["call"], best_long["put"]
            sc, sp = sh["call"], sh["put"]
            key = (lc["id"], lp["id"], sc["id"], sp["id"])
            if key in seen:
                continue
            seen.add(key)

            def _leg(lg, side):
                px = lg["px_buy"] if side == "buy" else lg["px_sell"]
                iv = lg["iv_buy"] if side == "buy" else lg["iv_sell"]
                lots = round(CONTRACT / lg["ratio"] / 1000, 3) if lg["source"] == "warrant" else 1
                return {
                    "leg": ("Long " if side == "buy" else "Short ") + lg["type"],
                    "source": lg["source"], "type": lg["type"], "K": round(lg["K"], 2),
                    "dte": lg["dte"], "id": lg["id"], "name": lg["name"],
                    "px_per_share": px, "iv": round(iv, 4), "ratio": lg["ratio"],
                    "board_lots": lots,  # 張 for a warrant leg; 1 = one option contract
                }

            net_cash = round((sh["px"] - best_long["px"]) * CONTRACT, 0)
            all_rows.append({
                "underlying_code": code,
                "underlying_price": round(S, 2),
                "long_call": _leg(lc, "buy"), "long_put": _leg(lp, "buy"),
                "short_call": _leg(sc, "sell"), "short_put": _leg(sp, "sell"),
                "long_iv": round(best_long["iv"], 4), "short_iv": round(sh["iv"], 4),
                "iv_edge_pts": round(edge_pts, 2),
                "long_cost_ps": round(best_long["px"], 4), "short_credit_ps": round(sh["px"], 4),
                "net_cash": net_cash,   # NT$ per 2000-share package; + = net credit
                "eff_k_long": round(best_long["K"], 2), "eff_k_short": round(sh["K"], 2),
                "k_diff_pct": round(abs(best_long["K"] - sh["K"]) / S * 100, 2),
                "t_long": best_long["dte"], "t_short": sh["dte"],
                "first_expiry": min(lc["dte"], lp["dte"], sc["dte"], sp["dte"]),
                "contract_size": CONTRACT,
                # Executability flags — any True means this row is debug-only.
                "loose_prices": bool(loose),
                "shorts_warrant": bool(sc["source"] == "warrant" or sp["source"] == "warrant"),
            })

    if not all_rows:
        why = (f"No straddle rows. Stages: legs={diag['legs']}, "
               f"long_pkgs={diag['longs']}, short_pkgs={diag['shorts']}, "
               f"pairs={diag['pairs']}, cut_by_dte_cover={diag['cut_dte_cover']}, "
               f"cut_by_min_iv_edge={diag['cut_edge']}")
        if diag["best_edge"] is not None:
            why += (f". Best edge found = {diag['best_edge']:.2f} pts vs "
                    f"threshold {min_iv_edge:.2f} pts — lower Min IV Edge to see it")
        elif diag["shorts"] == 0:
            why += (". No short packages: the short leg needs a live two-sided "
                    "option quote (or enable Debug: allow short warrants)")
        if errors:
            why += " | " + "; ".join(errors)
        raise RuntimeError(why)

    result = pd.DataFrame(all_rows).sort_values("iv_edge_pts", ascending=False)
    return result.head(200)


def _build_us_match_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff,
                       positive_loose=False, min_volume=0, strategy="same_type"):
    """Match Taiwan warrants against the same-underlying US ADR options.

    The US option leg is pre-converted to TWD-per-Taiwan-share by
    us_options_logic, so it lives in the same price space as the warrant leg.
    One US contract controls 100 * adr_ratio Taiwan shares (per listing), which
    drives warrants_needed = contract_size/ratio.

    strategy:
      - "same_type": direct match warrant to the SAME-type US option
        (_match_warrants_to_options).
      - "pcp": cross-market Put-Call Parity — price the warrant against the
        synthetic built from the OPPOSITE-type US option + the US ADR + a USD
        bond (_match_warrants_pcp with r=R_US, synthetic_underlying="option").
    """
    all_rows = []
    errors = []

    for code in stock_codes:
        if code not in us_options_logic.US_ADR_MAP:
            errors.append(f"{code}: no US ADR mapping")
            continue

        contract_size = us_options_logic.contract_tw_shares(code)  # TW shares/contract

        # No time-value cap and no IV solve on the arb path (see _build_arb_df).
        warrant_df, err = warrant_logic.fetch_warrants(
            [code], option_type, 0, 365, 0, 1e9, 0, compute_iv=False
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            continue

        try:
            # PCP pairs a warrant with the OPPOSITE-type option, so the option
            # fetch must include both types regardless of the warrant filter.
            opt_type_fetch = "All" if strategy == "pcp" else option_type
            opt_df = us_options_logic.fetch_us_options(code, opt_type_fetch, min_days=1, compute_iv=False)
            opt_df = opt_df[opt_df["is_live"]]
            if min_volume > 0:
                opt_df = opt_df[opt_df["volume"] >= min_volume]
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        if opt_df.empty:
            errors.append(f"{code}: no live US options")
            continue

        if strategy == "pcp":
            rows = _match_warrants_pcp(
                warrant_df, opt_df, contract_size, max_strike_diff_pct, max_dte_diff,
                positive_loose=positive_loose, r=us_options_logic.R_US,
                synthetic_underlying="option",
            )
        else:
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
    if strategy == "pcp" and "executable" in result.columns:
        # Executable arbs first, then by richest mispricing.
        result = result.sort_values(
            ["executable", "price_diff_pct"], ascending=[False, False]
        )
    elif "price_diff_pct" in result.columns:
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

        # Orderbook depth per leg. TW leg: best-level size (口) from TAIFEX MIS on
        # the side actually hit (buy TW@ask -> ask_size; sell TW@bid -> bid_size).
        # US leg: yfinance exposes no bid/ask size, so volume + OI stand in as a
        # liquidity proxy (NOT true resting depth).
        tw_size = tw.get("ask_size") if trade == "Long TW / Short US" else tw.get("bid_size")
        tw_size = int(tw_size) if pd.notna(tw_size) else None
        tw_fillable = tw_size is not None and tw_size >= int(tw_contracts)
        us_vol = int(us.get("volume") or 0)
        us_oi = int(us.get("oi") or 0)

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
            "tw_depth_contracts": tw_size,
            "tw_fillable": bool(tw_fillable),
            "us_volume": us_vol,
            "us_oi": us_oi,
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


@app.route("/universe_status")
def universe_status():
    # Kick/keep-alive the live ISIN scrape (also retries after a failed build),
    # then report progress for the "Building Warrant Universe" bar.
    warrant_logic._ensure_universe_fetch()
    return jsonify(warrant_logic.universe_status())


@app.route("/adr_premium", methods=["POST"])
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
def us_option_match():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = _build_us_match_df(stock_codes, option_type, max_strike_diff_pct,
                                max_dte_diff, positive_loose=positive_loose,
                                min_volume=min_volume, strategy=strategy)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/us_option_match_csv", methods=["POST"])
def us_option_match_csv():
    data = request.json
    stock_codes = data.get("stock_codes", ["2303"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    positive_loose = bool(data.get("positive_loose", False))
    min_volume = int(data.get("min_volume", 0) or 0)
    strategy = data.get("strategy", "same_type")
    try:
        df = _build_us_match_df(stock_codes, option_type, max_strike_diff_pct,
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


@app.route("/tw_us_option_match", methods=["POST"])
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
def straddle_arbitrage():
    p = _straddle_params(request.json)
    try:
        df = _build_straddle_df(**p)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/straddle_arbitrage_csv", methods=["POST"])
def straddle_arbitrage_csv():
    p = _straddle_params(request.json)
    try:
        df = _build_straddle_df(**p)
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


@app.route("/arb_finder", methods=["POST"])
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
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/arb_finder_csv", methods=["POST"])
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
    print("Step 1: resolving port", flush=True)
    port = _resolve_port(int(os.environ.get("PORT", 5001)))
    print(f"Step 2: starting browser timer (port {port})", flush=True)
    if not os.environ.get("RENDER"):
        threading.Timer(1.5, lambda: open_browser(port)).start()
    print("Step 3: starting cmoney key prefetch", flush=True)
    warrant_logic.prefetch_cmoney_key()
    print("Step 4: starting flask", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
