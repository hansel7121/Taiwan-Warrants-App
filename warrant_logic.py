import twstock
import requests
import json
import pandas as pd
import urllib3
from datetime import datetime
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def bs_price(S, K, T, r, sigma, ratio):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return ratio * (S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def bs_delta(S, K, T, r, sigma, ratio):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return norm.cdf(d1) * ratio


def calc_real_leverage(S, delta, ask):
    if ask <= 0:
        return 0.0
    return S * delta / ask


def implied_vol(price, S, K, T, r, ratio):
    if price <= 0 or T <= 0:
        return np.nan
    intrinsic = max(0, (S - K) * ratio)
    if price <= intrinsic:
        return np.nan
    try:
        return brentq(
            lambda sigma: bs_price(S, K, T, r, sigma, ratio) - price,
            1e-6,
            10.0,
            xtol=1e-6,
            maxiter=200,
        )
    except Exception:
        return np.nan


def get_warrant_info_batch(warrant_codes):
    url = "https://www.warrantwin.com.tw/eyuanta/ws/GetWarData.ashx"
    payload = {
        "format": "JSON",
        "factor": {
            "columns": [
                "FLD_WAR_ID",
                "FLD_WAR_NM",
                "FLD_OPTION_TYPE",
                "FLD_OBJ_TXN_PRICE",
                "FLD_WAR_BUY_PRICE",
                "FLD_WAR_SELL_PRICE",
                "FLD_DUR_END",
                "FLD_N_STRIKE_PRC",
                "FLD_N_UND_CONVER",
                "FLD_RISK_RATE_FREE",
            ],
            "condition": [
                {"field": "FLD_WAR_ID", "values": warrant_codes},
                {"field": "FLD_WAR_TYPE", "values": ["1", "2"]},
            ],
            "orderby": {
                "field": "FLD_WAR_TXN_VOLUME",
                "sort": "DESC",
                "agtfirst": "980",
            },
        },
        "pagination": {"row": len(warrant_codes), "page": "1"},
    }
    headers = {
        "Referer": "https://www.warrantwin.com.tw/eyuanta/Warrant/Info.aspx",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = requests.post(
        url, data={"data": json.dumps(payload)}, headers=headers, verify=False
    )
    results = r.json().get("result", [])

    rows = []
    for raw in results:
        try:
            expiry = datetime.strptime(raw["FLD_DUR_END"], "%Y%m%d")
            days_to_expiry = (expiry - datetime.today()).days
            underlying_price = float(raw["FLD_OBJ_TXN_PRICE"] or 0)
            ask = float(raw["FLD_WAR_SELL_PRICE"] or 0)
            bid = float(raw["FLD_WAR_BUY_PRICE"] or 0)
            strike = float(raw["FLD_N_STRIKE_PRC"] or 0)
            exercise_ratio = float(raw["FLD_N_UND_CONVER"] or 0)
            r_free = float(raw["FLD_RISK_RATE_FREE"] or 0) / 100

            if days_to_expiry <= 0 or ask <= 0 or bid <= 0:
                continue

            T = days_to_expiry / 365.0
            iv_ask = implied_vol(
                ask, underlying_price, strike, T, r_free, exercise_ratio
            )
            iv_bid = implied_vol(
                bid, underlying_price, strike, T, r_free, exercise_ratio
            )

            if np.isnan(iv_ask) or np.isnan(iv_bid):
                continue

            calc_delta = bs_delta(
                underlying_price, strike, T, r_free, iv_ask, exercise_ratio
            )
            calc_leverage = calc_real_leverage(underlying_price, calc_delta, ask)
            time_value = (ask / exercise_ratio) + strike - underlying_price

            rows.append(
                {
                    "warrant_code": raw["FLD_WAR_ID"],
                    "warrant_name": raw["FLD_WAR_NM"],
                    "type": "Put" if raw["FLD_OPTION_TYPE"] == "1" else "Call",
                    "underlying_price": underlying_price,
                    "ask": ask,
                    "bid": bid,
                    "days_to_expiry": days_to_expiry,
                    "strike": strike,
                    "exercise_ratio": exercise_ratio,
                    "time_value": round(time_value, 4),
                    "iv_ask": round(iv_ask, 4),
                    "iv_bid": round(iv_bid, 4),
                    "delta_calc": round(calc_delta, 4),
                    "leverage_calc": round(calc_leverage, 4),
                }
            )
        except Exception:
            continue

    return pd.DataFrame(rows)


def fetch_warrants(
    stock_codes, option_type="All", min_days=0, max_days=365, min_leverage=0.0
):
    today = datetime.today()

    if option_type == "Call":
        name_filter = "購"
    elif option_type == "Put":
        name_filter = "售"
    else:
        name_filter = None

    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    all_codes = []
    for stock_code in stock_codes:
        stock_info = twstock.codes.get(stock_code, None)
        if stock_info is None:
            continue
        name = stock_info.name
        codes = [
            k
            for k, v in twstock.codes.items()
            if "權證" in v.type
            and (name in v.name or stock_code in v.name)
            and (name_filter is None or name_filter in v.name)
            and datetime.strptime(v.start, "%Y/%m/%d") <= today
        ]
        all_codes.extend(codes)

    all_codes = list(set(all_codes))  # deduplicate

    if not all_codes:
        return pd.DataFrame(), "No warrants found"

    chunk_size = 200
    dfs = []
    for i in range(0, len(all_codes), chunk_size):
        chunk = all_codes[i : i + chunk_size]
        dfs.append(get_warrant_info_batch(chunk))

    df = pd.concat(dfs, ignore_index=True)
    df = df[(df["days_to_expiry"] >= min_days) & (df["days_to_expiry"] <= max_days)]
    df = df[df["leverage_calc"] >= min_leverage]
    if option_type != "All":
        df = df[df["type"] == option_type]

    return df, None


def fetch_iv_surface(stock_codes, option_type="All"):
    df, error = fetch_warrants(
        stock_codes, option_type=option_type, min_days=0, max_days=666, min_leverage=0.0
    )
    if df.empty:
        return None, error or "No data"

    df_clean = df[
        (df["iv_ask"] > 0.20)
        & (df["iv_ask"] < 1.00)
        & (df["days_to_expiry"] > 0)
        & (df["days_to_expiry"] < 666)
        & (abs(df["iv_ask"] - df["iv_bid"]) < 1)
    ].copy()

    if df_clean.empty:
        return None, "No warrants passed IV filter"

    return df_clean, None
