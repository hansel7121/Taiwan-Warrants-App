import twstock
from twstock.codes.fetch import (
    make_row_tuple,
    ROW,
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
from datetime import datetime, timezone
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
import json
import re
import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from services import applog
from services import memlog
from services import db_market

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CMONEY_URL = "https://www.cmoney.tw/finance/ashx/mainpage.ashx"
CMONEY_KEY_PAGE = "https://www.cmoney.tw/finance/warrantsquery.aspx?warrant=051666"
# The page carries one cmkey per warrant sub-page; anchor to the warrantsquery
# link so we take the key that mainpage.ashx?action=GetWarrantData accepts.
CMONEY_KEY_RE = re.compile(r"warrantsquery\.aspx'[^>]*cmkey='([^']+)'")
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
_cmoney_key_fetch_lock = threading.Lock()


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


def bs_vega(S, K, T, r, sigma, ratio):
    """Black-Scholes vega, per 1.00 (=100 vol-points) change in sigma, scaled by
    the instrument's exercise ratio. Mirrors the frontend `bsVega`. Vega does not
    depend on call/put, so no is_put arg (unlike bs_price / bs_delta)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return S * norm.pdf(d1) * np.sqrt(T) * ratio


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


def implied_vol_vec(price, S, K, T, r, ratio, is_put):
    """Vectorized implied_vol. Mirrors the scalar ``implied_vol`` exactly (after
    round(...,4)): NaN where price<=0, T<=0, or price<=intrinsic; otherwise the
    Black-Scholes IV in [1e-6, 10].

    All args are numpy arrays (or broadcastable). The easy 99% are solved by a
    single array Newton-Raphson sweep; any row that fails to converge, hits a
    bound, or whose residual is not tight enough falls back to the scalar
    ``implied_vol`` (brentq) in a short loop — so the hard cases agree with the
    scalar solver bit-for-bit and the common cases go ~10x+ faster.
    """
    price = np.asarray(price, dtype=float)
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    r = np.asarray(r, dtype=float)
    ratio = np.asarray(ratio, dtype=float)
    is_put = np.asarray(is_put, dtype=bool)
    price, S, K, T, r, ratio, is_put = np.broadcast_arrays(
        price, S, K, T, r, ratio, is_put
    )
    shape = price.shape
    out = np.full(shape, np.nan)
    pf = np.ascontiguousarray(price, dtype=float).ravel()
    Sf = np.ascontiguousarray(S, dtype=float).ravel()
    Kf = np.ascontiguousarray(K, dtype=float).ravel()
    Tf = np.ascontiguousarray(T, dtype=float).ravel()
    rf = np.ascontiguousarray(r, dtype=float).ravel()
    ratf = np.ascontiguousarray(ratio, dtype=float).ravel()
    ipf = np.ascontiguousarray(is_put, dtype=bool).ravel()

    # intrinsic matches the scalar exactly: put=max(0,(K-S)*ratio),
    # call=max(0,(S-K)*ratio). NaN unless price>0, T>0 and price>intrinsic.
    with np.errstate(all="ignore"):
        intrinsic = np.where(
            ipf,
            np.maximum(0.0, (Kf - Sf) * ratf),
            np.maximum(0.0, (Sf - Kf) * ratf),
        )
        valid = (pf > 0) & (Tf > 0) & (pf > intrinsic)
    idx = np.where(valid)[0]
    if idx.size == 0:
        return out

    p = pf[idx]; s = Sf[idx]; k = Kf[idx]; t = Tf[idx]
    rr = rf[idx]; rt = ratf[idx]; ip = ipf[idx]
    sqrtT = np.sqrt(t)
    logSK = np.log(s / k)

    def _model_and_vega(sig):
        d1 = (logSK + (rr + 0.5 * sig * sig) * t) / (sig * sqrtT)
        d2 = d1 - sig * sqrtT
        disc = np.exp(-rr * t)
        call = rt * (s * norm.cdf(d1) - k * disc * norm.cdf(d2))
        put = rt * (k * disc * norm.cdf(-d2) - s * norm.cdf(-d1))
        m = np.where(ip, put, call)
        vega = rt * s * sqrtT * norm.pdf(d1)
        return m, vega

    sig = np.full(idx.size, 0.5)
    with np.errstate(all="ignore"):
        for _ in range(50):
            sig = np.clip(sig, 1e-6, 10.0)
            m, vega = _model_and_vega(sig)
            diff = m - p
            if np.all(np.abs(diff) < 1e-10):
                break
            vega = np.where(np.abs(vega) < 1e-12, 1e-12, vega)
            sig = np.clip(sig - diff / vega, 1e-6, 10.0)
        m, vega_final = _model_and_vega(sig)
        resid = np.abs(m - p)
        # Scale-free vega: vega/(ratio*S) = sqrt(T)*pdf(d1). O(0.01-0.5) for a
        # well-conditioned root, ~1e-9 in flat deep-ITM/OTM bands where bs_price
        # is numerically insensitive to sigma over a wide range.
        norm_vega = np.abs(vega_final) / (rt * s)

    # Accept Newton only where it MUST agree with brentq after round(...,4):
    #   * price residual is tight (< 1e-9), and
    #   * the root is well-conditioned — norm_vega > 1e-6, so bs_price genuinely
    #     moves with sigma near the root. In flat/degenerate regions (deep ITM/
    #     OTM, price≈intrinsic) the residual can hit exactly 0 across a wide
    #     sigma band, so brentq and Newton settle on different points; those rows
    #     fall back to the scalar brentq and thus match the scalar solver
    #     exactly. Empirically this leaves only genuine .00005 rounding-boundary
    #     ties (|sigma diff| < 1e-6), which round identically in all tested cases.
    #   * sigma strictly inside the bracket (bound-pinned rows go to brentq too).
    accept = (
        (resid < 1e-9)
        & (norm_vega > 1e-6)
        & (sig > 1e-6)
        & (sig < 10.0)
    )
    res = np.full(idx.size, np.nan)
    res[accept] = sig[accept]
    for j in np.where(~accept)[0]:
        res[j] = implied_vol(
            float(p[j]), float(s[j]), float(k[j]), float(t[j]),
            float(rr[j]), float(rt[j]), bool(ip[j]),
        )
    # Rounding-edge safety for the IV column itself: any Newton-accepted row
    # whose round(...,4) could flip between Newton's ~exact root and brentq's
    # (root within ~1e-6 of a .00005 boundary) is re-solved with the scalar
    # brentq, so the returned array rounds bit-identically to the scalar solver.
    res = _refine_iv_for_rounding(res, p, s, k, t, rr, rt, ip)
    out.flat[idx] = res
    return out


def bs_delta_vec(S, K, T, r, sigma, ratio, is_put):
    """Vectorized bs_delta. Same semantics as the scalar: 0.0 where T<=0 or
    sigma<=0; NaN propagates where sigma is NaN with T>0 (matching the scalar,
    whose ``sigma<=0`` guard is False for NaN so it computes NaN)."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    r = np.asarray(r, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ratio = np.asarray(ratio, dtype=float)
    is_put = np.asarray(is_put, dtype=bool)
    S, K, T, r, sigma, ratio, is_put = np.broadcast_arrays(
        S, K, T, r, sigma, ratio, is_put
    )
    zero_mask = (T <= 0) | (sigma <= 0)
    with np.errstate(all="ignore"):
        d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * np.sqrt(T))
        val = np.where(is_put, (norm.cdf(d1) - 1.0) * ratio, norm.cdf(d1) * ratio)
    return np.where(zero_mask, 0.0, val)


def _refine_iv_for_rounding(iv, price, S, K, T, r, ratio, is_put,
                            check_delta=False, check_leverage=False,
                            lev_price=None, eps=5e-6):
    """Guarantee bit-exact parity of the 4-dp-rounded IV/delta/leverage columns
    against the scalar brentq path.

    ``iv`` is a vectorized-Newton result (NaN where unconverged). Newton's root
    is ~machine-precise and brentq's is within ~1e-6 of the true root, so
    |iv_newton - iv_brentq| < eps. For any row whose rounded IV (and optionally
    delta / leverage, computed exactly as the call site does) is UNCHANGED as iv
    moves across [iv-eps, iv+eps], both solvers must yield identical rounded
    columns. Only rows sitting on a rounding edge (the output flips within that
    window) are recomputed with the exact scalar brentq, so they match the
    scalar path bit-for-bit. This keeps the fast Newton path for the ~98% of
    rounding-stable rows while removing every derived-column boundary flip.
    """
    iv = np.array(iv, dtype=float)
    n = iv.shape[0]
    price = np.broadcast_to(np.asarray(price, dtype=float), (n,))
    S = np.broadcast_to(np.asarray(S, dtype=float), (n,))
    K = np.broadcast_to(np.asarray(K, dtype=float), (n,))
    T = np.broadcast_to(np.asarray(T, dtype=float), (n,))
    r = np.broadcast_to(np.asarray(r, dtype=float), (n,))
    ratio = np.broadcast_to(np.asarray(ratio, dtype=float), (n,))
    is_put = np.broadcast_to(np.asarray(is_put, dtype=bool), (n,))
    lev_price = price if lev_price is None else np.broadcast_to(
        np.asarray(lev_price, dtype=float), (n,))

    finite = ~np.isnan(iv)
    if not finite.any():
        return iv
    with np.errstate(all="ignore"):
        lo = np.clip(iv - eps, 1e-6, 10.0)
        hi = np.clip(iv + eps, 1e-6, 10.0)
        unstable = np.round(lo, 4) != np.round(hi, 4)
        if check_delta or check_leverage:
            d_lo = bs_delta_vec(S, K, T, r, lo, ratio, is_put)
            d_hi = bs_delta_vec(S, K, T, r, hi, ratio, is_put)
            if check_delta:
                unstable |= np.round(d_lo, 4) != np.round(d_hi, 4)
            if check_leverage:
                lev_lo = S * np.abs(d_lo) / lev_price
                lev_hi = S * np.abs(d_hi) / lev_price
                unstable |= np.round(lev_lo, 4) != np.round(lev_hi, 4)
    unstable &= finite
    out = iv.copy()
    for j in np.where(unstable)[0]:
        out[j] = implied_vol(float(price[j]), float(S[j]), float(K[j]),
                             float(T[j]), float(r[j]), float(ratio[j]),
                             bool(is_put[j]))
    return out


def _fetch_cmoney_key_http():
    """Scrape the cmkey token out of the warrantsquery page HTML.

    The key is rendered server-side into the nav links, so no JS execution is
    needed. It is not hardcoded because CMoney can rotate it; an Error:-3 from
    mainpage.ashx is the invalidation signal.
    """
    with memlog.measure("cmkey_http"):
        r = requests.get(CMONEY_KEY_PAGE, headers=CMONEY_HEADERS, verify=False,
                         timeout=10)
        r.raise_for_status()
        match = CMONEY_KEY_RE.search(r.text)
        if not match:
            raise RuntimeError("cmkey not found in warrantsquery page HTML")
        return match.group(1)


def _fetch_key_locked():
    """Fetch and store the key. Caller must hold _cmoney_key_fetch_lock."""
    global _cmoney_key
    print("KEY: fetching cmkey", flush=True)
    try:
        _cmoney_key = _fetch_cmoney_key_http()
        # Truncated: the key is a credential, but its prefix identifies which key
        # is live across a rotation.
        print(f"KEY: cmkey fetched ({applog.redact(_cmoney_key)})", flush=True)
    except Exception as e:
        print(f"KEY: cmkey fetch failed: {e}", flush=True)
        _cmoney_key = None
    return _cmoney_key


def prefetch_cmoney_key():
    with _cmoney_key_fetch_lock:
        return _fetch_key_locked()


def get_cmoney_key():
    global _cmoney_key
    if _cmoney_key is None:
        # Snapshot-first: the scheduler persists the live key to Supabase, so in
        # supabase mode use the stored key and only scrape if it is unavailable.
        if db_market.snapshot_enabled():
            try:
                stored = db_market.get_key()
            except Exception:
                stored = None
            if stored:
                _cmoney_key = stored
                return _cmoney_key
        # Double-checked: concurrent first requests share one fetch instead of
        # each hitting CMoney.
        with _cmoney_key_fetch_lock:
            if _cmoney_key is None:
                _fetch_key_locked()
    return _cmoney_key


def scrape_cmoney_key():
    global _cmoney_key
    with _cmoney_key_fetch_lock:
        _cmoney_key = None
        return _fetch_key_locked()


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
        applog.log("WARR", f"cmkey expired (Error -3) — refreshing key, retrying {len(codes)} codes")
        cmkey = scrape_cmoney_key()
        results = {}
        with ThreadPoolExecutor(max_workers=100) as executor:
            futures = {
                executor.submit(fetch_one_cmoney, code, cmkey): code for code in codes
            }
            for future in as_completed(futures):
                code, data = future.result()
                if data and data != "KEY_EXPIRED":
                    results[code] = data

    # Aggregate only: fetch_one_cmoney fans out over 100 threads, so a per-code
    # failure line would be thousands of lines for one request.
    applog.log(
        "WARR",
        f"cmoney {len(codes)} requested, {len(results)} ok, "
        f"{len(codes) - len(results)} failed",
    )
    return results


def build_warrant_df(cmoney_results, compute_iv=True, keep_noniv=False):
    r_free_default = 0.02

    # Pass 1: parse + pre-filter each warrant into a base row WITHOUT the IV
    # solve, collecting the pricing inputs in parallel arrays. The scalar solve
    # was the dominant cost; deferring it lets one vectorized sweep replace
    # thousands of per-row brentq calls.
    rows = []
    a_ask = []; a_bid = []; a_S = []; a_K = []; a_T = []; a_r = []; a_ratio = []; a_put = []
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
                    # IV-derived metrics filled in below (positions fixed here so
                    # the column order stays COL_ORDER regardless of solve path).
                    "iv_ask": np.nan,
                    "iv_bid": np.nan,
                    "delta_calc": np.nan,
                    "leverage_calc": np.nan,
                }
            )
            a_ask.append(ask); a_bid.append(bid); a_S.append(underlying_price)
            a_K.append(strike); a_T.append(T); a_r.append(r_free)
            a_ratio.append(exercise_ratio); a_put.append(is_put)
        except Exception:
            continue

    if not rows:
        return pd.DataFrame(columns=COL_ORDER)

    # Pass 2: one vectorized IV/delta/leverage solve over all surviving rows,
    # then the per-row post-logic (bid->ask fallback, keep_noniv, drop) applied
    # as array ops. round(...,4) is done element-wise with the builtin so the
    # rounding is byte-identical to the scalar path.
    n = len(rows)
    ask_arr = np.array(a_ask); bid_arr = np.array(a_bid); S_arr = np.array(a_S)
    K_arr = np.array(a_K); T_arr = np.array(a_T); r_arr = np.array(a_r)
    ratio_arr = np.array(a_ratio); put_arr = np.array(a_put, dtype=bool)

    if compute_iv:
        iv_ask = implied_vol_vec(ask_arr, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr)
        # Re-solve any row whose rounded delta OR leverage (both derived from the
        # unrounded iv_ask, exactly as the scalar path does) could flip between
        # Newton's and brentq's root, so those columns match the scalar path
        # bit-for-bit. Leverage denominator is the ask price.
        iv_ask = _refine_iv_for_rounding(
            iv_ask, ask_arr, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr,
            check_delta=True, check_leverage=True, lev_price=ask_arr,
        )
        bid_price = np.where(bid_arr > 0, bid_arr, np.nan)
        iv_bid = implied_vol_vec(bid_price, S_arr, K_arr, T_arr, r_arr, ratio_arr, put_arr)
        converged = ~np.isnan(iv_ask)
        # iv_bid falls back to iv_ask on converged rows where bid IV is NaN.
        iv_bid = np.where(converged & np.isnan(iv_bid), iv_ask, iv_bid)
        delta = bs_delta_vec(S_arr, K_arr, T_arr, r_arr, iv_ask, ratio_arr, put_arr)
        with np.errstate(all="ignore"):
            leverage = S_arr * np.abs(delta) / ask_arr  # ask>0 guaranteed above
        # Non-converged rows carry all-NaN IV metrics (drop or keep_noniv).
        iv_bid = np.where(converged, iv_bid, np.nan)
        delta = np.where(converged, delta, np.nan)
        leverage = np.where(converged, leverage, np.nan)
        keep = converged if not keep_noniv else np.ones(n, dtype=bool)
    else:
        # Arb finder does not use IV/delta/leverage — skip the solve entirely.
        iv_ask = iv_bid = delta = leverage = np.full(n, np.nan)
        keep = np.ones(n, dtype=bool)

    final_rows = []
    for i in range(n):
        if not keep[i]:
            continue
        d = rows[i]
        d["iv_ask"] = round(float(iv_ask[i]), 4)
        d["iv_bid"] = round(float(iv_bid[i]), 4)
        d["delta_calc"] = round(float(delta[i]), 4)
        d["leverage_calc"] = round(float(leverage[i]), 4)
        final_rows.append(d)

    if not final_rows:
        return pd.DataFrame(columns=COL_ORDER)
    return pd.DataFrame(final_rows)[COL_ORDER]


# ── Warrant result cache ─────────────────────────────────────────────────────
# Raw CMoney results cached per underlying so the background scheduler can
# refresh them off the request path. Fetch-on-miss: the first request after
# boot (or for an uncached stock) behaves exactly like the old live path.
_warrant_cache: dict = {}  # stock_code -> (timestamp, {warrant_code: result})
_warrant_cache_lock = threading.Lock()
WARRANT_CACHE_TTL = 1800  # safety margin over the 15-min scheduled refresh


# ── Listed-security universe ─────────────────────────────────────────────────
# twstock.codes is a snapshot bundled into the installed package at release
# time — months stale in practice, and it carries no expiry field, so it both
# misses new warrants and keeps long-expired ones. The ISIN listing it was
# scraped from enumerates *currently listed* securities, so re-scraping it live
# fixes both halves at once. Held in memory only: __update_codes() would rewrite
# CSVs inside site-packages, which is ephemeral on Render anyway.
_universe_codes: dict = {}  # code -> twstock fetch.ROW
_universe_ts = 0.0
_universe_fetching = False
_universe_fetch_started = 0.0
_universe_progress = 0        # 0-100, drives the frontend progress bar
_universe_error = None        # last scrape error message, or None
_universe_lock = threading.Lock()
UNIVERSE_TTL = 86400
# The ISIN scrape is slow and highly variable (2-6 min per market observed) and
# twstock's fetch_data passes no timeout, so a stalled socket could pin the
# in-flight flag forever. Age it out instead of blocking refreshes for good.
UNIVERSE_FETCH_STALL = 1800


def _malloc_trim():
    # glibc keeps freed lxml arenas out of the OS's hands; nudge it to release
    # them so the ~110MB parse transient doesn't become a permanent RSS floor.
    # Linux/glibc only — absent on macOS, where this is a harmless no-op.
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=False)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
    except Exception:
        pass


def _stream_isin(url, prog_band=None):
    """Stream-parse an ISIN listing into twstock ROW tuples without a full DOM.

    twstock's fetch_data builds one lxml tree over the ~7.7MB page; that single
    etree.HTML() call peaks ~110MB and, under glibc, never returns the freed
    arena to the OS — a permanent RSS floor. iterparse over <tr>, clearing each
    row as it closes, holds only one row at a time. Output is the identical ROW
    list fetch_data returns (same make_row_tuple, same header/type handling), so
    nothing downstream changes.

    The body is downloaded in ~64KB chunks so the progress bar can climb smoothly
    with bytes received (the download dominates the wall time). ``prog_band`` is
    ``(lo, hi, expected_bytes)``: progress sweeps lo→hi as the download lands
    (using Content-Length when the server sends it, else expected_bytes), then
    pins at hi once the buffer is complete. Omit it (the scheduler path) and no
    progress is reported.
    """
    global _universe_progress
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                     verify=False, timeout=60, stream=True)
    r.raise_for_status()
    # The ISIN listing is served as Big5/MS950 (its Content-Type charset) with
    # no <meta> charset. fetch_data decodes it through requests' r.text, which
    # honours the header charset (r.encoding). In stream mode r.apparent_encoding
    # would force-load the whole body (defeating streaming), so take the header
    # charset and fall back to cp950 (what these pages always are) instead of the
    # chardet sniff. A pinned "utf-8" mangles the ideographic space in each
    # "code　name" cell into U+FFFD, so make_row_tuple's split("　") returns
    # a single field and unpacking raises "not enough values to unpack (expected
    # 2, got 1)" on the very first data row. libxml2 needs an encoding name it
    # recognises, so normalise via the Python codec registry (MS950 -> cp950,
    # which it accepts); anything it can't resolve degrades to cp950.
    enc = r.encoding or "cp950"
    try:
        enc = codecs.lookup(enc).name
    except (LookupError, TypeError):
        enc = "cp950"
    lo, hi, exp = prog_band or (None, None, None)
    total = int(r.headers.get("Content-Length") or 0) or (exp or 0)
    buf = BytesIO()
    got = 0
    for chunk in r.iter_content(65536):
        if not chunk:
            continue
        buf.write(chunk)
        if prog_band:
            got += len(chunk)
            frac = min(1.0, got / total) if total else 0.0
            with _universe_lock:
                _universe_progress = int(lo + (hi - lo) * frac)
    rows = []
    typ = ""
    first = True
    context = etree.iterparse(BytesIO(buf.getvalue()), html=True,
                              encoding=enc, tag="tr")
    for _event, elem in context:
        if first:
            # The column-header <tr> (labels like 有價證券代號及名稱): fetch_data
            # drops it with xpath('//tr')[1:]; its cell has no 　 to split on.
            first = False
        else:
            cells = [x.text for x in elem.iter()]
            if len(cells) == 4:
                # Section header carrying the security type for the rows below.
                typ = cells[2].strip(" ")
            else:
                rows.append(make_row_tuple(typ, cells))
        # Clear the finished subtree and drop already-seen siblings so the tree
        # the parser retains stays empty — this is what keeps the peak flat.
        elem.clear()
        parent = elem.getparent()
        if parent is not None:
            while elem.getprevious() is not None:
                del parent[0]
    if prog_band:
        with _universe_lock:
            _universe_progress = hi
    return rows


def scrape_twse_universe():
    """Scheduler hook: re-scrape the ISIN listing and swap it into the cache."""
    global _universe_codes, _universe_ts, _universe_fetching, _universe_fetch_started
    global _universe_progress, _universe_error
    with _universe_lock:
        if _universe_fetching and time.time() - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            print("WARR: universe fetch already in flight", flush=True)
            return
        _universe_fetching = True
        _universe_fetch_started = time.time()
        _universe_progress = 4
        _universe_error = None
    try:
        # Sequential, not parallel: two lxml trees over ~31k rows at once is the
        # largest memory spike in the process and the host caps at 512 MB.
        print("WARR: universe fetch starting (~1 min)", flush=True)
        t0 = time.time()
        with memlog.measure("warrant_universe"):
            merged = {}
            # Each market owns a progress band and sweeps it smoothly by bytes
            # downloaded (expected sizes are the no-Content-Length fallback:
            # TWSE ~7.7MB, TPEX ~2.6MB).
            for market, url, lo, hi, exp in (
                ("twse", TWSE_EQUITIES_URL, 4, 64, 7_700_000),
                ("tpex", TPEX_EQUITIES_URL, 64, 98, 2_600_000),
            ):
                rows = _stream_isin(url, prog_band=(lo, hi, exp))
                print(f"WARR: universe {market} {len(rows)} rows", flush=True)
                for row in rows:
                    merged[row.code] = row
            # Hand the freed lxml arenas back to the OS while still inside the
            # measured block, so MEM: rss_after reflects the trimmed floor.
            _malloc_trim()
        if not merged:
            with _universe_lock:
                _universe_error = "ISIN listing returned no rows"
            print("WARR: universe fetch returned nothing — keeping fallback", flush=True)
            return
        fresh_w = sum(1 for v in merged.values() if "權證" in v.type)
        bundled_w = sum(1 for v in twstock.codes.values() if "權證" in v.type)
        with _universe_lock:
            # The results cached so far were resolved against whatever universe
            # was in effect — the bundled fallback on the first request after
            # boot. Replacing it silently would leave them stale for a full
            # WARRANT_CACHE_TTL, so decide here whether they must be dropped.
            in_effect = (
                _universe_codes
                if _universe_codes and time.time() - _universe_ts < UNIVERSE_TTL
                else twstock.codes
            )
            changed = merged.keys() != in_effect.keys()
            _universe_codes = merged
            _universe_ts = time.time()
            _universe_progress = 100
        # Cleared outside _universe_lock: these two locks are never nested
        # anywhere else, and nesting them here would be the only place that
        # could order them against the request path.
        if changed:
            with _warrant_cache_lock:
                dropped = len(_warrant_cache)
                _warrant_cache.clear()
            applog.log("WARR", f"universe changed, warrant cache "
                               f"invalidated ({dropped} entries dropped)")
        print(
            f"WARR: universe fetched {len(merged)} codes, {fresh_w} warrants "
            f"(bundled: {bundled_w}) in {time.time() - t0:.1f}s",
            flush=True,
        )
    except Exception as e:
        with _universe_lock:
            _universe_error = str(e)
        print(f"WARR: universe fetch failed: {e} — using bundled codes", flush=True)
    finally:
        with _universe_lock:
            _universe_fetching = False


def universe_rows():
    """Expose the in-memory merged universe as plain rows for the snapshot writer.

    Returns a list of {"code","name","start","market"} dicts from the current
    _universe_codes (twstock StockCodeInfo namedtuples). `start` is a
    "YYYY/MM/DD" string; normalise it to an ISO "YYYY-MM-DD" string (JSON-safe
    and castable by the md_warrant_universe.start date column), or None when it
    is missing/unparseable. Empty list if no universe has been scraped yet.
    """
    with _universe_lock:
        rows = list(_universe_codes.values())
    out = []
    for r in rows:
        try:
            start = datetime.strptime(r.start, "%Y/%m/%d").date().isoformat()
        except (ValueError, TypeError):
            start = None
        out.append({"code": r.code, "name": r.name, "start": start,
                     "market": r.market, "type": r.type})
    return out


def _universe_from_supabase():
    """Reconstruct the merged universe (ROW-shaped) from the Supabase snapshot.

    None if snapshot mode is off, the table is empty, or the read fails — the
    caller degrades to the bundled twstock snapshot in all of those cases.
    `start` round-trips through the DB as ISO ('YYYY-MM-DD'); convert back to
    twstock's native 'YYYY/MM/DD' since _warrant_codes_for parses that format.
    """
    try:
        df, _as_of = db_market.read_snapshot("warrant_universe")
    except Exception as e:
        applog.log_once("WARR", f"universe snapshot read failed: {e}",
                         "universe_snapshot_fail")
        return None
    if df.empty:
        return None
    if "type" not in df.columns:
        # Pre-migration snapshot (supabase/migrations/003_universe_type.sql not
        # yet applied): callers need `.type` to tell warrants from underlyings,
        # so a snapshot without it is unusable — fall back to bundled twstock.
        applog.log_once("WARR", "universe snapshot missing 'type' column "
                                 "(run migration 003) — skipping",
                         "universe_snapshot_no_type")
        return None
    out = {}
    for row in df.itertuples(index=False):
        start = row.start
        try:
            start = datetime.strptime(str(start)[:10], "%Y-%m-%d").strftime("%Y/%m/%d")
        except (ValueError, TypeError):
            start = None
        out[row.code] = ROW(row.type, row.code, row.name, None, start, row.market, None, None)
    return out


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


def _ensure_universe_fetch():
    """Kick a background scrape if the universe is stale and none is running.

    Never blocks: the scheduler is opt-in (ENABLE_SCHEDULER) and off in
    production, so this is what actually gets the fresh universe loaded there.
    Requests keep being served off the bundled fallback until it lands.
    """
    now = time.time()
    with _universe_lock:
        if _universe_codes and now - _universe_ts < UNIVERSE_TTL:
            return
        if _universe_fetching and now - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            return
    threading.Thread(target=scrape_twse_universe, daemon=True).start()


def _universe():
    """Fresh ISIN listing if we have one, else the bundled twstock snapshot.

    A TWSE outage must degrade to today's behaviour, never to an empty universe.

    Falling back is otherwise invisible: the only trace is a code count nobody
    can read as stale unless they already know both numbers, so say it outright.
    The happy path stays silent — 'fetching N codes' already covers it.
    """
    now = time.time()
    with _universe_lock:
        if _universe_codes and now - _universe_ts < UNIVERSE_TTL:
            return _universe_codes
        if _universe_fetching and now - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            why = "scrape in flight"
        elif not _universe_fetch_started:
            why = "no scrape yet"
        elif _universe_ts:
            why = "last scrape expired"
        else:
            why = "last scrape failed"
    if db_market.snapshot_enabled():
        snap = _universe_from_supabase()
        if snap:
            applog.log_once(
                "WARR",
                f"universe stale ({why}) — using Supabase snapshot "
                f"({len(snap)} codes)",
                "universe_fallback",
            )
            return snap
    applog.log_once(
        "WARR",
        f"universe stale ({why}) — using bundled twstock snapshot "
        f"({len(twstock.codes)} codes)",
        "universe_fallback",
    )
    return twstock.codes


def _warrant_codes_for(stock_codes):
    """Resolve each underlying to its full warrant-code universe (all types).

    Warrant names are "<underlying><issuer><serial>", e.g. 長榮鋼國票59購01.
    A plain prefix test leaks a longer-named stock's warrants into a shorter
    one (長榮 vs 長榮鋼). Disambiguate structurally: a warrant belongs to this
    underlying only if no *longer* real-security name (e.g. 長榮鋼, 長榮航) also
    prefixes the warrant name. This replaces a hand-maintained issuer-char
    whitelist that silently dropped every warrant of any issuer not listed.
    """
    _ensure_universe_fetch()
    codes = _universe()
    today = datetime.today()
    real_names = [v.name for v in codes.values() if "權證" not in v.type]

    def _make_matcher(name):
        # Longer real-security names that would also claim this warrant name.
        longer = [n for n in real_names if len(n) > len(name) and n.startswith(name)]

        def _name_matches(wname):
            if not wname.startswith(name):
                return False
            return not any(wname.startswith(n) for n in longer)

        return _name_matches

    code_map = {}
    for stock_code in stock_codes:
        stock_info = codes.get(stock_code, None)
        if stock_info is None:
            code_map[stock_code] = []
            continue
        name_matches = _make_matcher(stock_info.name)
        # Some underlyings appear in warrant names under an abbreviation that is
        # not the registered security name (e.g. ETF 元大台灣50 -> "台灣50"). The
        # authoritative CommKey check downstream drops wrong-underlying strays,
        # so the alias only needs to be permissive enough to fetch them.
        aliases = WARRANT_NAME_ALIASES.get(stock_code, [])
        code_map[stock_code] = [
            k
            for k, v in codes.items()
            if "權證" in v.type
            and (name_matches(v.name)
                 or v.name.startswith(stock_code)
                 or any(v.name.startswith(a) for a in aliases))
            and datetime.strptime(v.start, "%Y/%m/%d") <= today
        ]
    return code_map


def get_warrant_results(stock_codes, force=False):
    """Cached raw CMoney results for the given underlyings.

    Returns (results, as_of_ts, cached): merged {warrant_code: result}, the
    oldest cache timestamp involved, and whether everything was served from
    cache (False when any underlying was fetched live in this call).
    """
    now = time.time()
    with _warrant_cache_lock:
        need = [
            sc for sc in stock_codes
            if force or sc not in _warrant_cache
            or now - _warrant_cache[sc][0] >= WARRANT_CACHE_TTL
        ]
        hits = [
            (sc, int(now - _warrant_cache[sc][0]))
            for sc in stock_codes
            if sc not in need and sc in _warrant_cache
        ]
    for sc, age in hits:
        applog.log("WARR", f"{sc} cache hit (age {age}s)")
    if need:
        with memlog.measure("warrants_fetch"):
            code_map = _warrant_codes_for(need)
            all_codes = sorted({c for cs in code_map.values() for c in cs})
            applog.log(
                "WARR",
                f"{','.join(need)} fetching {len(all_codes)} codes"
                f"{' (forced)' if force else ''}",
            )
            t0 = time.time()
            fetched = get_cmoney_prices(all_codes) if all_codes else {}
            applog.log(
                "WARR",
                f"{','.join(need)} fetched {len(fetched)}/{len(all_codes)} codes "
                f"in {time.time() - t0:.1f}s",
            )
            ts = time.time()
            failed = []
            with _warrant_cache_lock:
                for sc in need:
                    sc_codes = code_map.get(sc, [])
                    sc_results = {c: fetched[c] for c in sc_codes if c in fetched}
                    # A stock that HAS warrant codes but returned none was a
                    # wholesale fetch failure (cmoney unreachable / cmkey fetch
                    # failed), not a stock with no warrants. Caching that empty
                    # result would pin "no warrants found" for WARRANT_CACHE_TTL
                    # and block retries, keeping the app dark long after cmoney
                    # recovers — so skip it and let the next request retry. Any
                    # prior good entry is left in place (its stale timestamp
                    # keeps it "expired", so it still serves last-known-good and
                    # retries). sc_codes empty == genuinely no warrants: cache it.
                    if sc_codes and not sc_results:
                        failed.append(sc)
                        continue
                    _warrant_cache[sc] = (ts, sc_results)
            if failed:
                applog.log(
                    "WARR",
                    f"{','.join(failed)} fetch failed (0/{len(all_codes)} codes ok) "
                    f"— not caching, will retry next request",
                )
    merged, as_of = {}, None
    with _warrant_cache_lock:
        for sc in stock_codes:
            ent = _warrant_cache.get(sc)
            if not ent:
                continue
            merged.update(ent[1])
            as_of = ent[0] if as_of is None else min(as_of, ent[0])
    return merged, as_of, not need


def cache_as_of(stock_codes):
    """Oldest warrant-cache timestamp (epoch) for these codes, or None."""
    with _warrant_cache_lock:
        ts = [_warrant_cache[sc][0] for sc in stock_codes if sc in _warrant_cache]
    return min(ts) if ts else None


def _apply_warrant_filters(df, stock_codes, option_type, min_days, max_days,
                           min_leverage, max_tv_pct, min_volume):
    """Downstream warrant filter chain (COL_ORDER select through option_type).

    Pure filtering: returns the filtered df (possibly empty). No logging / no
    tuple returns — callers own the empty-case messaging so the live path stays
    byte-identical and the supabase path can reuse the exact same filtering.
    """
    df = df[COL_ORDER]
    # Verify the true underlying: the name prefilter is intentionally permissive
    # (so no issuer is ever dropped), and abbreviated warrant names can point at
    # a different stock (e.g. 長榮太 -> 2645, not 長榮/2603). CommKey settles it.
    wanted = {str(c) for c in stock_codes}
    df = df[df["underlying_code"].astype(str).isin(wanted)]
    if df.empty:
        return df
    df = df[(df["days_to_expiry"] >= min_days) & (df["days_to_expiry"] <= max_days)]
    # leverage_calc is NaN when compute_iv=False; only filter when a real
    # threshold is set (NaN >= 0 is False and would wipe the whole frame).
    if float(min_leverage) > 0:
        df = df[df["leverage_calc"] >= float(min_leverage)]
    df = df[df["time_value_pct"] <= max_tv_pct]
    df = df[df["volume"] >= min_volume]
    if option_type != "All":
        df = df[df["type"] == option_type]
    return df


def read_warrant(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0.0,
    max_tv_pct=100.0,
    min_volume=0,
    compute_iv=True,
    keep_noniv=False,
):
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    # Snapshot-first read (MARKET_SOURCE=supabase). The stored snapshot is the
    # superset-with-IV; compute_iv=True (scanner) drops non-converged-IV rows to
    # reproduce the live scanner set, compute_iv=False (arb) keeps the superset.
    # Any error / empty snapshot falls through to the live path below.
    if db_market.snapshot_enabled():
        try:
            snap, as_of = db_market.read_snapshot("warrants", codes=stock_codes)
            if snap is not None and not snap.empty:
                meta = {"as_of": as_of, "cached": True}
                if compute_iv:
                    snap = snap[snap["iv_ask"].notna()]
                else:
                    # Live compute_iv=False emits NaN IV-derived metrics (no
                    # solve). The superset stored them; blank them so the frame —
                    # and every downstream consumer that branches on IV presence
                    # (arb_logic) — matches the live arb path exactly.
                    snap = snap.copy()
                    for _c in ("iv_ask", "iv_bid", "delta_calc", "leverage_calc"):
                        if _c in snap.columns:
                            snap[_c] = np.nan
                filtered = _apply_warrant_filters(
                    snap, stock_codes, option_type, min_days, max_days,
                    min_leverage, max_tv_pct, min_volume,
                )
                if filtered.empty:
                    return pd.DataFrame(), "No warrants for requested underlying", meta
                return filtered, None, meta
        except Exception as e:
            applog.log("WARR", f"supabase read failed ({e}) — falling back to live")

    return scrape_cmoney_warrant(
        stock_codes, option_type, min_days, max_days,
        min_leverage, max_tv_pct, min_volume, compute_iv, keep_noniv,
    )


def scrape_cmoney_warrant(
    stock_codes,
    option_type="All",
    min_days=0,
    max_days=365,
    min_leverage=0.0,
    max_tv_pct=100.0,
    min_volume=0,
    compute_iv=True,
    keep_noniv=False,
):
    """Pure live-scrape reader (no Supabase snapshot branch).

    The scraper half of read_warrant: always re-scrapes CMoney (force=True
    bypasses the in-process _warrant_cache so a manual refresh is guaranteed
    fresh) and applies the same filter chain. Returns the same
    (df, error, meta) tuple shape.
    """
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    cmoney_results, as_of, cached = get_warrant_results(stock_codes, force=True)
    meta = {
        "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None,
        "cached": cached,
    }

    codes_s = ",".join(stock_codes)
    if not cmoney_results:
        applog.log("WARR", f"{codes_s} -> 0 rows (no warrants found)")
        return pd.DataFrame(), "No warrants found", meta

    df = build_warrant_df(cmoney_results, compute_iv=compute_iv, keep_noniv=keep_noniv)

    if df.empty:
        applog.log(
            "WARR",
            f"{codes_s} -> 0 rows (none of {len(cmoney_results)} results survived build)",
        )
        return pd.DataFrame(), "No warrants passed filters", meta

    built = len(df)
    wanted = {str(c) for c in stock_codes}
    filtered = _apply_warrant_filters(
        df, stock_codes, option_type, min_days, max_days,
        min_leverage, max_tv_pct, min_volume,
    )
    # The only distinct intermediate return is "nothing matched the underlying";
    # COL_ORDER never drops rows, so an empty result with no underlying match is
    # exactly that case (preserves the pre-extraction message + log verbatim).
    if filtered.empty and not df["underlying_code"].astype(str).isin(wanted).any():
        applog.log(
            "WARR",
            f"{codes_s} -> 0 rows ({built} built, none matched the requested underlying)",
        )
        return pd.DataFrame(), "No warrants for requested underlying", meta
    df = filtered

    applog.log(
        "WARR",
        f"{codes_s} -> {len(df)} rows ({len(cmoney_results)} results, {built} built) "
        f"type={option_type} cached={cached}",
    )
    return df, None, meta


def fetch_iv_surface(stock_codes, option_type="All"):
    df, error, meta = read_warrant(
        stock_codes,
        option_type=option_type,
        min_days=0,
        max_days=666,
        min_leverage=0.0,
        max_tv_pct=100.0,
        min_volume=0,
    )
    if df.empty:
        return None, error or "No data", meta

    df_clean = df[
        (df["iv_ask"] > 0.20)
        & (df["iv_ask"] < 1.00)
        & (df["days_to_expiry"] > 0)
        & (df["days_to_expiry"] < 666)
        & (abs(df["iv_ask"] - df["iv_bid"]) < 1)
    ].copy()

    if df_clean.empty:
        return None, "No warrants passed IV filter", meta

    return df_clean, None, meta
