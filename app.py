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
import os
import json
import numpy as np
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


def open_browser():
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    threading.Timer(1.5, open_browser).start()
    app.run(debug=False)
