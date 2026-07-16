import twstock
from twstock.codes.fetch import fetch_data, TWSE_EQUITIES_URL, TPEX_EQUITIES_URL
import requests
import pandas as pd
import urllib3
from datetime import datetime
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
import json
import re
import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import applog
import memlog

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
    if _cmoney_key is None:
        # Double-checked: concurrent first requests share one fetch instead of
        # each hitting CMoney.
        with _cmoney_key_fetch_lock:
            if _cmoney_key is None:
                _fetch_key_locked()
    return _cmoney_key


def refresh_cmoney_key():
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

    # Aggregate only: fetch_one_cmoney fans out over 100 threads, so a per-code
    # failure line would be thousands of lines for one request.
    applog.log(
        "WARR",
        f"cmoney {len(codes)} requested, {len(results)} ok, "
        f"{len(codes) - len(results)} failed",
    )
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
_universe_lock = threading.Lock()
UNIVERSE_TTL = 86400
# The ISIN scrape is slow and highly variable (2-6 min per market observed) and
# twstock's fetch_data passes no timeout, so a stalled socket could pin the
# in-flight flag forever. Age it out instead of blocking refreshes for good.
UNIVERSE_FETCH_STALL = 1800


def refresh_warrant_universe():
    """Scheduler hook: re-scrape the ISIN listing and swap it into the cache."""
    global _universe_codes, _universe_ts, _universe_fetching, _universe_fetch_started
    with _universe_lock:
        if _universe_fetching and time.time() - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            print("WARR: universe fetch already in flight", flush=True)
            return
        _universe_fetching = True
        _universe_fetch_started = time.time()
    try:
        # Sequential, not parallel: two lxml trees over ~31k rows at once is the
        # largest memory spike in the process and the host caps at 512 MB.
        print("WARR: universe fetch starting (~1 min)", flush=True)
        t0 = time.time()
        with memlog.measure("warrant_universe"):
            merged = {}
            for market, url in (("twse", TWSE_EQUITIES_URL), ("tpex", TPEX_EQUITIES_URL)):
                rows = fetch_data(url)
                print(f"WARR: universe {market} {len(rows)} rows", flush=True)
                for row in rows:
                    merged[row.code] = row
        if not merged:
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
        print(f"WARR: universe fetch failed: {e} — using bundled codes", flush=True)
    finally:
        with _universe_lock:
            _universe_fetching = False


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
    threading.Thread(target=refresh_warrant_universe, daemon=True).start()


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
            with _warrant_cache_lock:
                for sc in need:
                    _warrant_cache[sc] = (
                        ts,
                        {c: fetched[c] for c in code_map.get(sc, []) if c in fetched},
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


def refresh_warrant_cache(stock_codes):
    """Scheduler hook: force-refetch the given underlyings into the cache."""
    get_warrant_results(stock_codes, force=True)


def cache_as_of(stock_codes):
    """Oldest warrant-cache timestamp (epoch) for these codes, or None."""
    with _warrant_cache_lock:
        ts = [_warrant_cache[sc][0] for sc in stock_codes if sc in _warrant_cache]
    return min(ts) if ts else None


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
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    cmoney_results, as_of, cached = get_warrant_results(stock_codes)
    meta = {
        "as_of": datetime.fromtimestamp(as_of).isoformat() if as_of else None,
        "cached": cached,
    }

    codes_s = ",".join(stock_codes)
    if not cmoney_results:
        applog.log("WARR", f"{codes_s} -> 0 rows (no warrants found)")
        return pd.DataFrame(), "No warrants found", meta

    df = build_warrant_df(cmoney_results, compute_iv=compute_iv)

    if df.empty:
        applog.log(
            "WARR",
            f"{codes_s} -> 0 rows (none of {len(cmoney_results)} results survived build)",
        )
        return pd.DataFrame(), "No warrants passed filters", meta

    built = len(df)
    df = df[COL_ORDER]
    # Verify the true underlying: the name prefilter is intentionally permissive
    # (so no issuer is ever dropped), and abbreviated warrant names can point at
    # a different stock (e.g. 長榮太 -> 2645, not 長榮/2603). CommKey settles it.
    wanted = {str(c) for c in stock_codes}
    df = df[df["underlying_code"].astype(str).isin(wanted)]
    if df.empty:
        applog.log(
            "WARR",
            f"{codes_s} -> 0 rows ({built} built, none matched the requested underlying)",
        )
        return pd.DataFrame(), "No warrants for requested underlying", meta
    df = df[(df["days_to_expiry"] >= min_days) & (df["days_to_expiry"] <= max_days)]
    # leverage_calc is NaN when compute_iv=False; only filter when a real
    # threshold is set (NaN >= 0 is False and would wipe the whole frame).
    if float(min_leverage) > 0:
        df = df[df["leverage_calc"] >= float(min_leverage)]
    df = df[df["time_value_pct"] <= max_tv_pct]
    df = df[df["volume"] >= min_volume]
    if option_type != "All":
        df = df[df["type"] == option_type]

    applog.log(
        "WARR",
        f"{codes_s} -> {len(df)} rows ({len(cmoney_results)} results, {built} built) "
        f"type={option_type} cached={cached}",
    )
    return df, None, meta


def fetch_iv_surface(stock_codes, option_type="All"):
    df, error, meta = fetch_warrants(
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
