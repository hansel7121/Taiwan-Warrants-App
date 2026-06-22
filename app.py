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


def _build_arb_df(option_type, max_strike_diff_pct, max_dte_diff):
    warrant_df, err = warrant_logic.fetch_warrants(
        ["2330"], option_type, 0, 365, 0, 100, 0
    )
    if warrant_df.empty:
        raise RuntimeError(err or "No TSMC warrants found")

    opt_df = options_logic.fetch_options(["2330"], option_type, min_days=1)
    if opt_df.empty:
        raise RuntimeError("No TSMC options found")

    rows = []
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

        candidates = candidates[
            (candidates["strike_diff_pct"] <= max_strike_diff_pct)
            & (candidates["dte_diff"] <= max_dte_diff)
        ]
        if candidates.empty:
            continue

        # Strike weighted 2× so closest strike wins over closest DTE
        score = candidates["strike_diff_pct"] * 2 + candidates["dte_diff"] / max(max_dte_diff, 1)
        best = candidates.loc[score.idxmin()]

        ratio = float(w["exercise_ratio"])
        warrants_needed = round(1000 / ratio)
        warrant_per_share = round(float(w["ask"]) / ratio, 4)
        opt_per_share = round(float(best["ask"]), 4)
        price_diff = round(opt_per_share - warrant_per_share, 4)
        price_diff_pct = (
            round(price_diff / opt_per_share * 100, 2) if opt_per_share > 0 else None
        )

        rows.append(
            {
                "warrant_code": w["warrant_code"],
                "warrant_name": w["warrant_name"],
                "option_contract": best["contract"],
                "type": w["type"],
                "underlying_price": w["underlying_price"],
                "warrant_dte": int(w["days_to_expiry"]),
                "opt_dte": int(best["days_to_expiry"]),
                "dte_diff": int(best["dte_diff"]),
                "warrant_strike": w["strike"],
                "opt_strike": int(best["strike"]),
                "strike_diff_pct": round(float(best["strike_diff_pct"]), 2),
                "warrants_needed": warrants_needed,
                "warrant_ask": w["ask"],
                "opt_ask": best["ask"],
                "warrant_per_share": warrant_per_share,
                "opt_per_share": opt_per_share,
                "price_diff": price_diff,
                "price_diff_pct": price_diff_pct,
                "warrant_iv": round(float(w["iv_ask"]), 4) if pd.notna(w["iv_ask"]) else None,
                "opt_iv": round(float(best["iv_ask"]), 4) if pd.notna(best["iv_ask"]) else None,
                "iv_diff": round(
                    (float(best["iv_ask"]) if pd.notna(best["iv_ask"]) else 0)
                    - (float(w["iv_ask"]) if pd.notna(w["iv_ask"]) else 0),
                    4,
                ),
            }
        )

    result = pd.DataFrame(rows)
    if not result.empty and "price_diff_pct" in result.columns:
        result = result.sort_values("price_diff_pct", ascending=False)
    return result


@app.route("/arb_finder", methods=["POST"])
def arb_finder():
    data = request.json
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    try:
        df = _build_arb_df(option_type, max_strike_diff_pct, max_dte_diff)
        rows = json.loads(df.to_json(orient="records")) if not df.empty else []
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return jsonify({"rows": [], "count": 0, "error": str(e)})


@app.route("/arb_finder_csv", methods=["POST"])
def arb_finder_csv():
    data = request.json
    option_type = data.get("option_type", "All")
    max_strike_diff_pct = float(data.get("max_strike_diff_pct", 3.0))
    max_dte_diff = int(data.get("max_dte_diff", 5))
    try:
        df = _build_arb_df(option_type, max_strike_diff_pct, max_dte_diff)
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
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    print("Step 1: starting browser timer", flush=True)
    threading.Timer(1.5, open_browser).start()
    print("Step 2: starting cmoney key prefetch", flush=True)
    warrant_logic.prefetch_cmoney_key()
    print("Step 3: starting flask", flush=True)
    app.run(debug=False)
