import twstock
from twstock.codes.fetch import (
    make_row_tuple,
    TWSE_EQUITIES_URL,
    TPEX_EQUITIES_URL,
)
import requests
import codecs
import ctypes
import ctypes.util
from io import BytesIO
from lxml import etree
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
import time
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

# Warrant-name abbreviations that differ from the registered security name.
# Used only to widen the fetch prefilter; CommKey verification then confirms the
# true underlying, so an over-broad alias is safe.
WARRANT_NAME_ALIASES = {
    "0050": ["台灣50"],   # 元大台灣50 ETF -> warrants named "台灣50..."
}

COL_ORDER = [
    "warrant_code",
    "warrant_name",
    "underlying_code",
    "type",
    "underlying_price",
    "ask",
    "bid",
    "ask_qty",
    "bid_qty",
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
    # Warm the live warrant universe at startup too, so the ~1-min ISIN scrape
    # is already done before the user's first scan (which would otherwise race
    # it and fall back to the stale bundled twstock snapshot).
    _ensure_universe_fetch()


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


# Thread-local pooled sessions: reusing keep-alive connections avoids a fresh
# TLS handshake per warrant, which dominates fetch time (~10x speedup on large
# warrant universes like 2330). One Session per worker thread (requests.Session
# is not guaranteed thread-safe to share across threads).
_thread_local = threading.local()


def _cmoney_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return s


def fetch_one_cmoney(code, cmkey):
    try:
        r = _cmoney_session().get(
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


def build_warrant_df(cmoney_results, compute_iv=True):
    r_free_default = 0.02
    rows = []

    for code, data in cmoney_results.items():
        try:
            w = data["Warrant"]
            s = data["Stock"]

            # CMoney's Stock.CommKey is the authoritative underlying stock code
            # (e.g. "2645"), so the true underlying is verified here rather than
            # inferred from the abbreviated warrant display name.
            underlying_code = str(s.get("CommKey")) if s.get("CommKey") is not None else None
            underlying_price = float(s.get("SalePr") or 0)
            ask = float(w.get("SellPr1") or 0)
            bid = float(w.get("BuyPr1") or 0)
            # Best-level orderbook size (張). CMoney returns the full 5-level
            # depth (SellQty1..5 / BuyQty1..5); we keep only level 1 — the size
            # actually resting at the best ask/bid — so the arb finder can check
            # whether an arb's needed board_lots can be filled at the quoted price.
            ask_qty = int(w.get("SellQty1") or 0)   # 張 resting at best ask
            bid_qty = int(w.get("BuyQty1") or 0)    # 張 resting at best bid
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

            if compute_iv:
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
            else:
                # Arb finder does not use IV/delta/leverage — skip the solve so a
                # leg is never dropped just because IV wouldn't converge, and no
                # time is wasted on it.
                iv_ask = iv_bid = calc_delta = calc_leverage = np.nan

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
                    "underlying_code": underlying_code,
                    "type": "Put" if is_put else "Call",
                    "underlying_price": underlying_price,
                    "ask": ask,
                    "bid": bid,
                    "ask_qty": ask_qty,
                    "bid_qty": bid_qty,
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


# ── Listed-security universe (live ISIN re-scrape) ───────────────────────────
# twstock.codes is a snapshot bundled into the installed package at release
# time — months stale in practice, and it carries no expiry field, so it both
# MISSES newly issued warrants and KEEPS long-expired ones. The ISIN listing it
# was scraped from enumerates *currently listed* securities, so re-scraping it
# live fixes both halves at once. Held in memory only (no site-packages writes).
# The scrape runs in a background thread kicked at startup; the warrant search
# REQUIRES the live universe and refuses to serve until it lands (the frontend
# shows a "Building Warrant Universe" progress bar). The bundled twstock snapshot
# is kept only as a dormant emergency fallback (see _universe()) and is NOT used
# on the normal fetch path.
_universe_codes: dict = {}   # code -> twstock fetch.ROW (same shape as twstock.codes values)
_universe_ts = 0.0
_universe_fetching = False
_universe_fetch_started = 0.0
_universe_progress = 0        # 0-100, drives the frontend progress bar
_universe_error = None        # last scrape error message, or None
_universe_lock = threading.Lock()
UNIVERSE_TTL = 86400          # re-scrape at most once a day
UNIVERSE_FETCH_STALL = 1800   # age out a stuck in-flight scrape after 30 min


def _malloc_trim():
    # glibc keeps freed lxml arenas out of the OS's hands; nudge it to release
    # them so the parse transient doesn't become a permanent RSS floor. Linux/
    # glibc only — a harmless no-op on macOS.
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=False)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
    except Exception:
        pass


def _stream_isin(url):
    """Stream-parse an ISIN listing into twstock ROW tuples without a full DOM.

    iterparse over <tr>, clearing each row as it closes, holds only one row at a
    time (twstock's fetch_data builds one large lxml tree instead). Output is the
    identical ROW list fetch_data returns (same make_row_tuple, same header/type
    handling), so nothing downstream changes.
    """
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                     verify=False, timeout=60)
    r.raise_for_status()
    rows = []
    typ = ""
    first = True
    # The ISIN listing is Big5/MS950 with no <meta> charset. Mirror requests'
    # resolution (header charset, else a chardet sniff) and normalise through the
    # Python codec registry to a name libxml2 accepts (MS950 -> cp950); a pinned
    # utf-8 would mangle the ideographic space separating "code　name".
    enc = r.encoding or r.apparent_encoding
    try:
        enc = codecs.lookup(enc).name
    except (LookupError, TypeError):
        enc = "utf-8"
    context = etree.iterparse(BytesIO(r.content), html=True, encoding=enc, tag="tr")
    for _event, elem in context:
        if first:
            first = False           # skip the column-header <tr>
        else:
            cells = [x.text for x in elem.iter()]
            if len(cells) == 4:
                typ = cells[2].strip(" ")   # section header carrying the security type
            else:
                rows.append(make_row_tuple(typ, cells))
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]
    return rows


def refresh_warrant_universe():
    """Re-scrape the TWSE + TPEX ISIN listings and swap them into the universe."""
    global _universe_codes, _universe_ts, _universe_fetching, _universe_fetch_started
    global _universe_progress, _universe_error
    with _universe_lock:
        if _universe_fetching and time.time() - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            return
        _universe_fetching = True
        _universe_fetch_started = time.time()
        _universe_progress = 4
        _universe_error = None
    try:
        t0 = time.time()
        merged = {}
        # Sequential, not parallel: two lxml trees at once is the biggest memory
        # spike in the process. Each market bumps the progress bar as it lands.
        for _market, url, pct in (("twse", TWSE_EQUITIES_URL, 55),
                                  ("tpex", TPEX_EQUITIES_URL, 92)):
            for row in _stream_isin(url):
                merged[row.code] = row
            with _universe_lock:
                _universe_progress = pct
        _malloc_trim()
        if not merged:
            with _universe_lock:
                _universe_error = "ISIN listing returned no rows"
            print("WARR: universe scrape returned nothing", flush=True)
            return
        with _universe_lock:
            _universe_codes = merged
            _universe_ts = time.time()
            _universe_progress = 100
        fresh_w = sum(1 for v in merged.values() if "權證" in v.type)
        print(f"WARR: universe scraped {len(merged)} codes, {fresh_w} warrants "
              f"in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        with _universe_lock:
            _universe_error = str(e)
        print(f"WARR: universe scrape failed: {e}", flush=True)
    finally:
        with _universe_lock:
            _universe_fetching = False


def _ensure_universe_fetch():
    """Kick a background scrape if the universe is stale and none is running.

    Non-blocking. Also acts as the retry hook: after a failed scrape (fetching
    flag cleared, no fresh data) the next call restarts it.
    """
    now = time.time()
    with _universe_lock:
        if _universe_codes and now - _universe_ts < UNIVERSE_TTL:
            return
        if _universe_fetching and now - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            return
    threading.Thread(target=refresh_warrant_universe, daemon=True).start()


def _universe_ready():
    """True once a fresh live ISIN scrape is loaded."""
    with _universe_lock:
        return bool(_universe_codes) and time.time() - _universe_ts < UNIVERSE_TTL


def universe_status():
    """Progress-bar payload for the frontend: ready / building / progress / error."""
    with _universe_lock:
        ready = bool(_universe_codes) and time.time() - _universe_ts < UNIVERSE_TTL
        return {
            "ready": ready,
            "building": _universe_fetching and not ready,
            "progress": 100 if ready else _universe_progress,
            "codes": len(_universe_codes),
            "warrants": sum(1 for v in _universe_codes.values() if "權證" in v.type),
            "error": _universe_error,
        }


def _universe():
    """EMERGENCY fallback only — the bundled twstock snapshot.

    Kept in the codebase so a manual/emergency path can still resolve codes if
    the live listing is unreachable, but the normal fetch_warrants path no longer
    calls this: it requires the fresh scrape (_universe_ready) and makes the user
    wait rather than silently serving stale, expiry-less bundled data.
    """
    now = time.time()
    with _universe_lock:
        if _universe_codes and now - _universe_ts < UNIVERSE_TTL:
            return _universe_codes
    return twstock.codes


def fetch_warrants(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0.0,
    max_tv_pct=100.0,
    min_volume=0,
    compute_iv=True,
):
    today = datetime.today()
    # Require the live ISIN universe — never serve the stale bundled snapshot.
    # If the startup scrape hasn't landed yet, refuse and let the caller wait
    # (the frontend shows a "Building Warrant Universe" progress bar).
    _ensure_universe_fetch()
    if not _universe_ready():
        return pd.DataFrame(), "Building warrant universe… please wait"
    with _universe_lock:
        codes = _universe_codes

    if option_type == "Call":
        name_filter = "購"
    elif option_type == "Put":
        name_filter = "售"
    else:
        name_filter = None

    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    # Warrant names are "<underlying><issuer><serial>", e.g. 長榮鋼國票59購01.
    # A plain prefix test leaks a longer-named stock's warrants into a shorter
    # one (長榮 vs 長榮鋼). Disambiguate structurally: a warrant belongs to this
    # underlying only if no *longer* real-security name (e.g. 長榮鋼, 長榮航) also
    # prefixes the warrant name. This replaces a hand-maintained issuer-char
    # whitelist that silently dropped every warrant of any issuer not listed.
    real_names = [v.name for v in codes.values() if "權證" not in v.type]

    def _make_matcher(name):
        # Longer real-security names that would also claim this warrant name.
        longer = [n for n in real_names if len(n) > len(name) and n.startswith(name)]

        def _name_matches(wname):
            if not wname.startswith(name):
                return False
            return not any(wname.startswith(n) for n in longer)

        return _name_matches

    all_codes = []
    for stock_code in stock_codes:
        stock_info = codes.get(stock_code, None)
        if stock_info is None:
            continue
        name = stock_info.name
        name_matches = _make_matcher(name)
        # Some underlyings appear in warrant names under an abbreviation that is
        # not the registered security name (e.g. ETF 元大台灣50 -> "台灣50"). The
        # authoritative CommKey check below drops any wrong-underlying strays, so
        # the alias here only needs to be permissive enough to fetch them.
        aliases = WARRANT_NAME_ALIASES.get(stock_code, [])
        matched = [
            k
            for k, v in codes.items()
            if "權證" in v.type
            and (name_matches(v.name)
                 or v.name.startswith(stock_code)
                 or any(v.name.startswith(a) for a in aliases))
            and (name_filter is None or name_filter in v.name)
            and datetime.strptime(v.start, "%Y/%m/%d") <= today
        ]
        all_codes.extend(matched)

    all_codes = list(set(all_codes))

    if not all_codes:
        return pd.DataFrame(), "No warrants found"

    cmoney_results = get_cmoney_prices(all_codes)

    if not cmoney_results:
        return pd.DataFrame(), "No active warrants found"

    df = build_warrant_df(cmoney_results, compute_iv=compute_iv)

    if df.empty:
        return pd.DataFrame(), "No warrants passed filters"

    df = df[COL_ORDER]
    # Verify the true underlying: the name prefilter is intentionally permissive
    # (so no issuer is ever dropped), and abbreviated warrant names can point at
    # a different stock (e.g. 長榮太 -> 2645, not 長榮/2603). CommKey settles it.
    wanted = {str(c) for c in stock_codes}
    df = df[df["underlying_code"].astype(str).isin(wanted)]
    if df.empty:
        return pd.DataFrame(), "No warrants for requested underlying"
    df = df[(df["days_to_expiry"] >= min_days) & (df["days_to_expiry"] <= max_days)]
    # leverage_calc is NaN when compute_iv=False; only filter when a real
    # threshold is set (NaN >= 0 is False and would wipe the whole frame).
    if float(min_leverage) > 0:
        df = df[df["leverage_calc"] >= float(min_leverage)]
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
