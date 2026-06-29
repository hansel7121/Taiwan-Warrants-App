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
import os
import json
import numpy as np
import pandas as pd
from scipy.interpolate import griddata

base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
app = Flask(
    __name__,
    template_folder=os.path.join(base_dir, "templates"),
    static_folder=os.path.join(base_dir, "static"),
)

app.json.sort_keys = False

CUSTOM_STOCKS_FILE = os.path.join(base_dir, "custom_stocks.json")


@app.route("/")
def index():
    return render_template("index.html")


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
                               max_strike_diff_pct, max_dte_diff):
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
            candidates = candidates[
                (candidates["strike_diff_pct"] <= max_strike_diff_pct)
                & (candidates["dte_diff"] <= max_dte_diff)
                & strike_filter
            ]
            if candidates.empty:
                continue

            score = candidates["strike_diff_pct"] * 2 + candidates["dte_diff"] / max(max_dte_diff, 1)
            best = candidates.loc[score.idxmin()]

            ratio = float(w["exercise_ratio"])
            warrant_ask_per_share = round(float(w["ask"]) / ratio, 4)
            opt_bid_per_share = round(float(best["bid"]), 4)
            opt_ask_per_share = round(float(best["ask"]), 4)

            price_diff = round(opt_bid_per_share - warrant_ask_per_share, 4)

            # Only keep pairs where the sign matches the intended trade direction.
            # If the sign is wrong the payoff at expiry is not risk-free and the
            # P&L chart will show negative values — these are not arb opportunities.
            if direction == "positive" and price_diff <= 0:
                continue
            if direction == "negative" and price_diff >= 0:
                continue

            pair_key = (w["warrant_code"], best["contract"])
            if pair_key in seen:
                continue
            seen.add(pair_key)

            warrants_needed = round(opt_contract_size / ratio)
            warrant_bid_per_share = round(float(w["bid"]) / ratio, 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else warrant_ask_per_share

            price_diff_pct = (
                round(price_diff / opt_bid_per_share * 100, 2) if opt_bid_per_share > 0 else None
            )

            if direction == "positive":
                trade = "Buy Warrant / Sell Option"
            else:
                trade = "Buy Option / Sell Warrant"

            rows.append({
                "warrant_code": w["warrant_code"],
                "warrant_name": w["warrant_name"],
                "option_contract": best["contract"],
                "type": w["type"],
                "trade": trade,
                "underlying_price": w["underlying_price"],
                "warrant_dte": int(w["days_to_expiry"]),
                "opt_dte": int(best["days_to_expiry"]),
                "dte_diff": int(best["dte_diff"]),
                "warrant_strike": w["strike"],
                "opt_strike": int(best["strike"]),
                "strike_diff_pct": round(float(best["strike_diff_pct"]), 2),
                "warrants_needed": warrants_needed,
                "warrant_ask": w["ask"],
                "warrant_bid": round(float(w["bid"]), 4) if pd.notna(w.get("bid")) and float(w.get("bid", 0)) > 0 else None,
                "opt_bid": opt_bid_per_share,
                "opt_ask": opt_ask_per_share,
                "warrant_per_share": warrant_ask_per_share,
                "opt_per_share": opt_bid_per_share,
                "price_diff": price_diff,
                "price_diff_pct": price_diff_pct,
                "warrant_iv": round(float(w["iv_ask"]), 4) if pd.notna(w["iv_ask"]) else None,
                "opt_iv": round(float(best["iv_bid"]), 4) if pd.notna(best.get("iv_bid")) else None,
                "iv_diff": round(
                    (float(best["iv_bid"]) if pd.notna(best.get("iv_bid")) else 0)
                    - (float(w["iv_ask"]) if pd.notna(w["iv_ask"]) else 0), 4,
                ),
            })
    return rows


def _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff):
    all_rows = []
    errors = []

    for code in stock_codes:
        if code not in options_logic.COMMODITY_MAP:
            errors.append(f"{code}: no options data available")
            continue

        cfg = options_logic.COMMODITY_MAP[code]
        opt_contract_size = cfg["exercise_ratio"]

        warrant_df, err = warrant_logic.fetch_warrants(
            [code], option_type, 0, 365, 0, 100, 0
        )
        if warrant_df.empty:
            errors.append(f"{code}: {err or 'no warrants'}")
            continue

        try:
            opt_df = options_logic.fetch_options([code], option_type, min_days=1)
            opt_df = opt_df[opt_df["is_live"]]
        except Exception as e:
            errors.append(f"{code}: {e}")
            continue

        if opt_df.empty:
            errors.append(f"{code}: no live options")
            continue

        rows = _match_warrants_to_options(
            warrant_df, opt_df, opt_contract_size, max_strike_diff_pct, max_dte_diff
        )
        all_rows.extend(rows)

    if not all_rows:
        msg = "; ".join(errors) if errors else "No matches found"
        raise RuntimeError(msg)

    result = pd.DataFrame(all_rows)
    if "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result


R_FREE = 0.01875


def _build_arb_pcp_df(option_type, max_strike_diff_pct, max_dte_diff):
    warrant_df, err = warrant_logic.fetch_warrants(
        ["2330"], option_type, 0, 365, 0, 100, 0
    )
    if warrant_df.empty:
        raise RuntimeError(err or "No TSMC warrants found")

    # Fetch all options (both Call and Put) for cross-type matching
    opt_df = options_logic.fetch_options(["2330"], "All", min_days=1)
    opt_df = opt_df[opt_df["is_live"]]
    if opt_df.empty:
        raise RuntimeError("No TSMC options with live bid/ask found")

    rows = []
    for _, w in warrant_df.iterrows():
        w_type = w["type"]
        opposite = "Put" if w_type == "Call" else "Call"

        candidates = opt_df[opt_df["type"] == opposite].copy()
        if candidates.empty:
            continue

        candidates["strike_diff_pct"] = (
            (candidates["strike"] - w["strike"]).abs() / w["strike"] * 100
        )
        candidates["dte_diff"] = (
            (candidates["days_to_expiry"] - w["days_to_expiry"]).abs()
        )
        candidates = candidates[
            (candidates["strike_diff_pct"] <= max_strike_diff_pct)
            & (candidates["dte_diff"] <= max_dte_diff)
        ]
        if candidates.empty:
            continue

        score = candidates["strike_diff_pct"] * 2 + candidates["dte_diff"] / max(max_dte_diff, 1)
        best = candidates.loc[score.idxmin()]

        S = float(w["underlying_price"])
        K = float(best["strike"])
        T = float(best["days_to_expiry"]) / 365.0
        ratio = float(w["exercise_ratio"])
        # Buy warrant at ask; receive option bid when selling the opposite leg
        opt_bid = float(best["bid"])
        opt_ask = float(best["ask"])   # kept for display
        warrant_per_share = float(w["ask"]) / ratio
        bond_pv = K * np.exp(-R_FREE * T)

        if w_type == "Call":
            # Synthetic call = S - K·e^(-rT) + Put(bid)
            synthetic_price = S - bond_pv + opt_bid
        else:
            # Synthetic put = K·e^(-rT) - S + Call(bid)
            synthetic_price = bond_pv - S + opt_bid

        pcp_diff = synthetic_price - warrant_per_share
        pcp_diff_pct = (
            round(pcp_diff / abs(synthetic_price) * 100, 2)
            if synthetic_price != 0 else None
        )

        rows.append(
            {
                "warrant_code": w["warrant_code"],
                "warrant_name": w["warrant_name"],
                "option_contract": best["contract"],
                "warrant_type": w_type,
                "opt_type": opposite,
                "underlying_price": round(S, 2),
                "warrant_dte": int(w["days_to_expiry"]),
                "opt_dte": int(best["days_to_expiry"]),
                "dte_diff": int(best["dte_diff"]),
                "warrant_strike": w["strike"],
                "opt_strike": int(K),
                "strike_diff_pct": round(float(best["strike_diff_pct"]), 2),
                "warrants_needed": round(2000 / ratio),
                "warrant_ask": w["ask"],
                "opt_bid": round(opt_bid, 4),   # price received when selling
                "opt_ask": round(opt_ask, 4),   # reference
                "warrant_per_share": round(warrant_per_share, 4),
                "bond_pv": round(bond_pv, 4),
                "synthetic_price": round(synthetic_price, 4),
                "pcp_diff": round(pcp_diff, 4),
                "pcp_diff_pct": pcp_diff_pct,
                "warrant_iv": round(float(w["iv_ask"]), 4) if pd.notna(w["iv_ask"]) else None,
                "opt_iv": round(float(best["iv_ask"]), 4) if pd.notna(best["iv_ask"]) else None,
            }
        )

    result = pd.DataFrame(rows)
    if not result.empty and "pcp_diff_pct" in result.columns:
        result = result.sort_values("pcp_diff_pct", ascending=False)
    return result


@app.route("/arb_pcp", methods=["POST"])
def arb_pcp():
    data = request.json
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    try:
        df = _build_arb_pcp_df(option_type, max_strike_diff_pct, max_dte_diff)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/arb_pcp_csv", methods=["POST"])
def arb_pcp_csv():
    data = request.json
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    try:
        df = _build_arb_pcp_df(option_type, max_strike_diff_pct, max_dte_diff)
    except Exception:
        df = pd.DataFrame()
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=arb_pcp.csv"},
    )


@app.route("/arb_finder", methods=["POST"])
def arb_finder():
    data = request.json
    stock_codes = data.get("stock_codes", ["2330"])
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    try:
        df = _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff)
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
    try:
        df = _build_arb_df(stock_codes, option_type, max_strike_diff_pct, max_dte_diff)
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


def open_browser():
    port = int(os.environ.get("PORT", 5001))
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    print("Step 1: starting browser timer", flush=True)
    if not os.environ.get("RENDER"):
        threading.Timer(1.5, open_browser).start()
    print("Step 2: starting cmoney key prefetch", flush=True)
    warrant_logic.prefetch_cmoney_key()
    print("Step 3: starting flask", flush=True)
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
