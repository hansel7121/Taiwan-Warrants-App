import twstock
import requests
import pandas as pd
import urllib3
from datetime import datetime
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
import json
import re
import asyncio
import threading
import os
import sys

if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.dirname(sys.executable), "ms-playwright"
    )
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CMONEY_URL = "https://www.cmoney.tw/finance/ashx/mainpage.ashx"
CMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.cmoney.tw/finance/warrantsquery.aspx",
}

COL_ORDER = [
    "warrant_code",
    "warrant_name",
    "type",
    "underlying_price",
    "ask",
    "bid",
    "days_to_expiry",
    "strike",
    "exercise_ratio",
    "volume",
    "time_value",
    "time_value_pct",
    "time_value_am",
    "iv_ask",
    "iv_bid",
    "delta_calc",
    "leverage_calc",
]

_cmoney_key = None
_cmoney_key_event = threading.Event()


def bs_price(S, K, T, r, sigma, ratio, is_put=False):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if is_put:
        return ratio * (K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return ratio * (S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def bs_delta(S, K, T, r, sigma, ratio, is_put=False):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    if is_put:
        return (norm.cdf(d1) - 1) * ratio
    return norm.cdf(d1) * ratio


def calc_real_leverage(S, delta, ask):
    if ask <= 0:
        return 0.0
    return S * delta / ask


def implied_vol(price, S, K, T, r, ratio, is_put=False):
    if price <= 0 or T <= 0:
        return np.nan
    intrinsic = max(0, (K - S) * ratio) if is_put else max(0, (S - K) * ratio)
    if price <= intrinsic:
        return np.nan
    try:
        return brentq(
            lambda sigma: bs_price(S, K, T, r, sigma, ratio, is_put) - price,
            1e-6,
            10.0,
            xtol=1e-6,
            maxiter=200,
        )
    except Exception:
        return np.nan


async def _fetch_cmoney_key_async():
    print("PW: launching chromium", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        print("PW: browser launched", flush=True)
        context = await browser.new_context()
        page = await context.new_page()
        cmkey = None

        def handle_request(request):
            nonlocal cmkey
            if "mainpage.ashx" in request.url and "cmkey" in request.url:
                match = re.search(r"cmkey=([^&]+)", request.url)
                if match:
                    import urllib.parse

                    cmkey = urllib.parse.unquote(match.group(1))

        page.on("request", handle_request)
        await page.goto(
            "https://www.cmoney.tw/finance/warrantsquery.aspx?warrant=051666"
        )
        print("PW: page loaded, waiting for key", flush=True)
        for _ in range(100):
            if cmkey:
                break
            await asyncio.sleep(0.1)

        print(f"PW: done, key={'found' if cmkey else 'NOT found'}", flush=True)
        await browser.close()
        return cmkey


def _background_fetch_key():
    global _cmoney_key
    print("BG: starting key fetch", flush=True)
    try:
        _cmoney_key = asyncio.run(_fetch_cmoney_key_async())
        print("BG: key fetched successfully", flush=True)
    except Exception as e:
        print(f"BG: key fetch failed: {e}", flush=True)
        _cmoney_key = None
    finally:
        _cmoney_key_event.set()


def prefetch_cmoney_key():
    t = threading.Thread(target=_background_fetch_key, daemon=True)
    t.start()


def get_cmoney_key():
    global _cmoney_key
    if _cmoney_key is None:
        _cmoney_key_event.wait(timeout=30)
    return _cmoney_key


def refresh_cmoney_key():
    global _cmoney_key
    _cmoney_key_event.clear()
    _cmoney_key = None
    _background_fetch_key()
    return _cmoney_key


def fetch_one_cmoney(code, cmkey):
    try:
        r = requests.get(
            CMONEY_URL,
            params={
                "action": "GetWarrantData",
                "cmkey": cmkey,
                "commKey": code,
            },
            headers=CMONEY_HEADERS,
            verify=False,
            timeout=5,
        )
        data = r.json()
        if "Warrant" in data and "Stock" in data:
            return code, data
        if data.get("Error") == -3:
            return code, "KEY_EXPIRED"
    except Exception:
        pass
    return code, None


def get_cmoney_prices(codes):
    global _cmoney_key
    cmkey = get_cmoney_key()

    results = {}
    key_expired = False

    with ThreadPoolExecutor(max_workers=100) as executor:
        futures = {
            executor.submit(fetch_one_cmoney, code, cmkey): code for code in codes
        }
        for future in as_completed(futures):
            code, data = future.result()
            if data == "KEY_EXPIRED":
                key_expired = True
            elif data is not None:
                results[code] = data

    if key_expired:
        cmkey = refresh_cmoney_key()
        results = {}
        with ThreadPoolExecutor(max_workers=100) as executor:
            futures = {
                executor.submit(fetch_one_cmoney, code, cmkey): code for code in codes
            }
            for future in as_completed(futures):
                code, data = future.result()
                if data and data != "KEY_EXPIRED":
                    results[code] = data

    return results


def build_warrant_df(cmoney_results):
    r_free_default = 0.02
    rows = []

    for code, data in cmoney_results.items():
        try:
            w = data["Warrant"]
            s = data["Stock"]

            underlying_price = float(s.get("SalePr") or 0)
            ask = float(w.get("SellPr1") or 0)
            bid = float(w.get("BuyPr1") or 0)
            volume = int(w.get("SaleQty") or 0)
            warrant_name = w.get("CommName", "")
            days_to_expiry = int(w.get("LastDays") or 0)
            strike = float(w.get("StrikePr") or 0)
            exercise_ratio = float(w.get("UserRate") or 0)
            r_free = r_free_default

            is_put = int(w.get("CallorPut") or 1) == 2

            if ask <= 0 or underlying_price <= 0 or days_to_expiry <= 0:
                continue

            T = days_to_expiry / 365.0

            iv_ask = implied_vol(
                ask, underlying_price, strike, T, r_free, exercise_ratio, is_put
            )
            iv_bid = (
                implied_vol(bid, underlying_price, strike, T, r_free, exercise_ratio, is_put)
                if bid > 0
                else np.nan
            )

            if np.isnan(iv_ask):
                continue
            if np.isnan(iv_bid):
                iv_bid = iv_ask

            calc_delta = bs_delta(
                underlying_price, strike, T, r_free, iv_ask, exercise_ratio, is_put
            )
            calc_leverage = calc_real_leverage(underlying_price, abs(calc_delta), ask)

            if is_put:
                intrinsic = max(0, strike - underlying_price) * exercise_ratio
                time_value = (ask / exercise_ratio) + underlying_price - strike
            else:
                intrinsic = max(0, underlying_price - strike) * exercise_ratio
                time_value = (ask / exercise_ratio) + strike - underlying_price
            time_value_am = ask - intrinsic

            rows.append(
                {
                    "warrant_code": code,
                    "warrant_name": warrant_name,
                    "type": "Put" if is_put else "Call",
                    "underlying_price": underlying_price,
                    "ask": ask,
                    "bid": bid,
                    "days_to_expiry": days_to_expiry,
                    "strike": strike,
                    "exercise_ratio": exercise_ratio,
                    "volume": volume,
                    "time_value": round(time_value, 4),
                    "time_value_pct": round(time_value / underlying_price * 100, 4)
                    if underlying_price > 0
                    else 0,
                    "time_value_am": round(time_value_am, 4),
                    "iv_ask": round(iv_ask, 4),
                    "iv_bid": round(iv_bid, 4),
                    "delta_calc": round(calc_delta, 4),
                    "leverage_calc": round(calc_leverage, 4),
                }
            )
        except Exception:
            continue

    if not rows:
        return pd.DataFrame(columns=COL_ORDER)
    return pd.DataFrame(rows)[COL_ORDER]


def fetch_warrants(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0.0,
    max_tv_pct=100.0,
    min_volume=0,
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

    all_codes = list(set(all_codes))

    if not all_codes:
        return pd.DataFrame(), "No warrants found"

    cmoney_results = get_cmoney_prices(all_codes)

    if not cmoney_results:
        return pd.DataFrame(), "No active warrants found"

    df = build_warrant_df(cmoney_results)

    if df.empty:
        return pd.DataFrame(), "No warrants passed filters"

    df = df[COL_ORDER]
    df = df[(df["days_to_expiry"] >= min_days) & (df["days_to_expiry"] <= max_days)]
    df = df[df["leverage_calc"] >= min_leverage]
    df = df[df["time_value_pct"] <= max_tv_pct]
    df = df[df["volume"] >= min_volume]
    if option_type != "All":
        df = df[df["type"] == option_type]

    return df, None


def fetch_iv_surface(stock_codes, option_type="All"):
    df, error = fetch_warrants(
        stock_codes,
        option_type=option_type,
        min_days=0,
        max_days=666,
        min_leverage=0.0,
        max_tv_pct=100.0,
        min_volume=0,
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
