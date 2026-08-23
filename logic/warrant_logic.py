"""CMoney warrant fetch and warrant-frame construction: quote parsing, the
IV/delta/leverage solve (kernels re-exported from logic.iv_engine), and the live
cmkey/underlying-universe scrape that resolves warrant codes to their true
underlying stock.
"""
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
import json
import re
import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from services import applog
from services import memlog
from services import db_market
from logic.ttl_cache import TTLCache
from logic.warrant_frame_py import COL_ORDER  # noqa: F401  (both engines need it)
from logic.iv_engine import (  # noqa: F401
    # The warrant-frame builder: Rust when built, logic/warrant_frame_py.py
    # otherwise. Re-exported so read_warrant and every test keep one name.
    build_warrant_df,
    # Black-Scholes kernels, re-exported so every existing
    # `from logic.warrant_logic import implied_vol, ...` call site is unchanged.
    # logic/iv_engine.py picks the compiled Rust engine (warrants_core) when it
    # imports and the pure-Python reference in logic/bs_python.py otherwise.
    bs_price, bs_delta, bs_vega, calc_real_leverage,
    implied_vol, implied_vol_vec, bs_delta_vec, _refine_iv_for_rounding,
    ENGINE as IV_ENGINE, engine_info as iv_engine_info,
)

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


_cmoney_key = None
_cmoney_key_fetch_lock = threading.Lock()


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
    """Returns (code, data_or_KEY_EXPIRED_or_None, exception_message_or_None).

    The exception message is carried out instead of being swallowed: a network /
    CMoney outage otherwise looks identical to "this code simply has no
    warrants", and the scanner ends up reporting a silent 0 rows.
    """
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
            return code, data, None
        if data.get("Error") == -3:
            return code, "KEY_EXPIRED", None
    except Exception as e:
        return code, None, str(e)
    return code, None, None


def get_cmoney_prices(codes, errors_out=None):
    """Fetch raw CMoney payloads for `codes`.

    `errors_out`, when given a list, receives the distinct exception messages
    raised while fetching — the caller uses them to tell a real outage from a
    genuinely empty result. Kept as an out-param so the (results) return shape
    stays what every existing caller expects.
    """
    global _cmoney_key
    cmkey = get_cmoney_key()

    results = {}
    errors = {}
    key_expired = False

    with ThreadPoolExecutor(max_workers=100) as executor:
        futures = {
            executor.submit(fetch_one_cmoney, code, cmkey): code for code in codes
        }
        for future in as_completed(futures):
            code, data, err = future.result()
            if data == "KEY_EXPIRED":
                key_expired = True
            elif data is not None:
                results[code] = data
            elif err:
                errors[err] = errors.get(err, 0) + 1

    if key_expired:
        applog.log("WARR", f"cmkey expired (Error -3) — refreshing key, retrying {len(codes)} codes",
                   level="WARN")
        cmkey = scrape_cmoney_key()
        results = {}
        errors = {}
        with ThreadPoolExecutor(max_workers=100) as executor:
            futures = {
                executor.submit(fetch_one_cmoney, code, cmkey): code for code in codes
            }
            for future in as_completed(futures):
                code, data, err = future.result()
                if data and data != "KEY_EXPIRED":
                    results[code] = data
                elif err:
                    errors[err] = errors.get(err, 0) + 1

    # Aggregate only: fetch_one_cmoney fans out over 100 threads, so a per-code
    # failure line would be thousands of lines for one request. Exceptions are
    # deduplicated by message for the same reason.
    applog.log(
        "WARR",
        f"cmoney {len(codes)} requested, {len(results)} ok, "
        f"{len(codes) - len(results)} failed",
        level="ERROR" if (errors and not results) else "INFO",
    )
    if errors:
        applog.log(
            "WARR",
            "cmoney fetch errors: " + "; ".join(f"{m} (x{n})" for m, n in errors.items()),
            level="ERROR" if not results else "WARN",
        )
        if errors_out is not None:
            errors_out.extend(f"{m} (x{n})" for m, n in errors.items())
    return results


# ── Warrant result cache ─────────────────────────────────────────────────────
# Raw CMoney results cached per underlying so the background scheduler can
# refresh them off the request path. Fetch-on-miss: the first request after
# boot (or for an uncached stock) behaves exactly like the old live path.
WARRANT_CACHE_TTL = 1800  # safety margin over the 15-min scheduled refresh
_warrant_cache = TTLCache("warrants", WARRANT_CACHE_TTL)  # stock_code -> {warrant_code: result}


# ── Listed-security universe ─────────────────────────────────────────────────
# twstock.codes is a snapshot bundled into the installed package at release
# time — months stale in practice, and it carries no expiry field, so it both
# misses new warrants and keeps long-expired ones. The ISIN listing it was
# scraped from enumerates *currently listed* securities, so re-scraping it live
# fixes both halves at once. Held in memory only: __update_codes() would rewrite
# CSVs inside site-packages, which is ephemeral on Render anyway.
UNIVERSE_TTL = 86400
# Single-keyed: one entry ("all") holding the whole {code -> twstock fetch.ROW}
# map, swapped atomically by the scraper.
_universe_cache = TTLCache("warrant_universe", UNIVERSE_TTL)
_universe_fetching = False
_universe_fetch_started = 0.0
_universe_progress = 0        # 0-100, drives the frontend progress bar
_universe_error = None        # last scrape error message, or None
# Guards the scrape's progress/in-flight state only; the codes themselves live
# in _universe_cache, which does its own locking.
_universe_lock = threading.Lock()
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
    # Page is Big5/MS950 with no <meta> charset; use the header charset (not
    # apparent_encoding, which would defeat streaming) and normalize it to a
    # name libxml2 accepts. A pinned "utf-8" would mangle the ideographic space
    # in each "code　name" cell, breaking make_row_tuple's split.
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
    global _universe_fetching, _universe_fetch_started
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
        # The results cached so far were resolved against whatever universe was
        # in effect — the bundled fallback on the first request after boot.
        # Replacing it silently would leave them stale for a full
        # WARRANT_CACHE_TTL, so decide here whether they must be dropped.
        current = _universe_cache.fresh("all")
        in_effect = current[1] if current else twstock.codes
        changed = merged.keys() != in_effect.keys()
        _universe_cache.set("all", merged)
        with _universe_lock:
            _universe_progress = 100
        if changed:
            dropped = _warrant_cache.invalidate()
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
    scraped universe (twstock StockCodeInfo namedtuples). `start` is a
    "YYYY/MM/DD" string; normalise it to an ISO "YYYY-MM-DD" string (JSON-safe
    and castable by the md_warrant_universe.start date column), or None when it
    is missing/unparseable. Empty list if no universe has been scraped yet.
    """
    entry = _universe_cache.entry("all")
    rows = list(entry[1].values()) if entry else []
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
    entry = _universe_cache.entry("all")
    codes = entry[1] if entry else {}
    ready = _universe_cache.fresh("all") is not None
    with _universe_lock:
        return {
            "ready": ready,
            "building": _universe_fetching and not ready,
            "progress": 100 if ready else _universe_progress,
            "codes": len(codes),
            "warrants": sum(1 for v in codes.values() if "權證" in v.type),
            "error": _universe_error,
        }


def _universe():
    """Fresh ISIN listing if we have one, else the bundled twstock snapshot.

    A TWSE outage must degrade to today's behaviour, never to an empty universe.

    Falling back is otherwise invisible: the only trace is a code count nobody
    can read as stale unless they already know both numbers, so say it outright.
    The happy path stays silent — 'fetching N codes' already covers it.
    """
    now = time.time()
    hit = _universe_cache.fresh("all")
    if hit is not None:
        return hit[1]
    scraped = _universe_cache.entry("all")
    with _universe_lock:
        if _universe_fetching and now - _universe_fetch_started < UNIVERSE_FETCH_STALL:
            why = "scrape in flight"
        elif not _universe_fetch_started:
            why = "no scrape yet"
        elif scraped:
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


def get_warrant_results(stock_codes, force=False, errors_out=None):
    """Cached raw CMoney results for the given underlyings.

    Returns (results, as_of_ts, cached): merged {warrant_code: result}, the
    oldest cache timestamp involved, and whether everything was served from
    cache (False when any underlying was fetched live in this call).

    `errors_out`, when given a list, collects genuine CMoney fetch failures
    (see get_cmoney_prices) so the caller can report an outage as an error
    rather than as an empty result.
    """
    now = time.time()
    # One snapshot of the cache for the need/hit split, so the two lists are
    # decided against the same state.
    snap = {k: ts for k, ts, _v in _warrant_cache.items()}
    need = [
        sc for sc in stock_codes
        if force or sc not in snap or now - snap[sc] >= WARRANT_CACHE_TTL
    ]
    hits = [
        (sc, int(now - snap[sc]))
        for sc in stock_codes
        if sc not in need and sc in snap
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
            fetched = get_cmoney_prices(all_codes, errors_out=errors_out) if all_codes else {}
            applog.log(
                "WARR",
                f"{','.join(need)} fetched {len(fetched)}/{len(all_codes)} codes "
                f"in {time.time() - t0:.1f}s",
            )
            ts = time.time()
            failed = []
            for sc in need:
                sc_codes = code_map.get(sc, [])
                sc_results = {c: fetched[c] for c in sc_codes if c in fetched}
                # A stock with codes but no results is a fetch failure, not "no
                # warrants" — skip caching it so the next request retries instead
                # of pinning a false empty for WARRANT_CACHE_TTL. Any prior good
                # entry is left in place, stale but still served as last-known-good.
                if sc_codes and not sc_results:
                    failed.append((sc, len(sc_codes)))
                    continue
                # One shared `ts` across the batch: as_of below takes the min.
                _warrant_cache.set(sc, sc_results, ts=ts)
            if failed:
                # Count per stock, not the whole batch: "0/5793" made one
                # 3-warrant stock look like a total outage.
                detail = ", ".join(f"{sc} (0/{n} codes ok)" for sc, n in failed)
                applog.log(
                    "WARR",
                    f"{detail} fetch failed — not caching, will retry next request",
                    level="ERROR",
                )
    merged, as_of = {}, None
    for sc in stock_codes:
        # entry(), not fresh(): an expired entry is still served as
        # last-known-good when the refetch above failed.
        ent = _warrant_cache.entry(sc)
        if not ent:
            continue
        merged.update(ent[1])
        as_of = ent[0] if as_of is None else min(as_of, ent[0])
    return merged, as_of, not need


def cache_as_of(stock_codes):
    """Oldest warrant-cache timestamp (epoch) for these codes, or None."""
    ts = [
        ent[0] for ent in (_warrant_cache.entry(sc) for sc in stock_codes)
        if ent is not None
    ]
    return min(ts) if ts else None


def _apply_warrant_filters(df, stock_codes, option_type, min_days, max_days,
                           min_leverage, max_tv_pct, min_volume,
                           allow_no_quote=False):
    """Downstream warrant filter chain (COL_ORDER select through option_type).

    Pure filtering: returns the filtered df (possibly empty). No logging / no
    tuple returns — callers own the empty-case messaging so the live path stays
    byte-identical and the supabase path can reuse the exact same filtering.
    """
    # reindex, not df[COL_ORDER]: a snapshot written before the bid/ask
    # time-value split lacks those columns, and NaN there degrades gracefully
    # (blank cells) instead of raising until the next scheduler write.
    df = df.reindex(columns=COL_ORDER)
    # Zero-ask warrants exist only for the scanner. Re-dropping them here is
    # what protects every other consumer (arb, suggestions) that reads the same
    # superset snapshot with allow_no_quote=False.
    if not allow_no_quote:
        df = df[df["ask"].fillna(0) > 0]
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
    # NaN ask TV% means there is no ask to measure; a max-TV cap can't judge it,
    # so it passes rather than silently dropping every no-ask row.
    df = df[df["ask_time_value_pct"].isna() | (df["ask_time_value_pct"] <= max_tv_pct)]
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
    allow_no_quote=False,
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
                    # Mirrors build_warrant_df's `keep`: no-ask rows have no
                    # price to solve, so a plain notna() drop would erase them.
                    converged = snap["iv_ask"].notna()
                    if allow_no_quote:
                        converged = converged | (snap["ask"].fillna(0) <= 0)
                    snap = snap[converged]
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
                    allow_no_quote=allow_no_quote,
                )
                if filtered.empty:
                    return pd.DataFrame(), "No warrants for requested underlying", meta
                return filtered, None, meta
        except Exception as e:
            applog.log("WARR", f"supabase read failed ({e}) — falling back to live")

    return scrape_cmoney_warrant(
        stock_codes, option_type, min_days, max_days,
        min_leverage, max_tv_pct, min_volume, compute_iv, keep_noniv,
        allow_no_quote,
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
    allow_no_quote=False,
):
    """Pure live-scrape reader (no Supabase snapshot branch).

    The scraper half of read_warrant: always re-scrapes CMoney (force=True
    bypasses the in-process _warrant_cache so a manual refresh is guaranteed
    fresh) and applies the same filter chain. Returns the same
    (df, error, meta) tuple shape.
    """
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]

    fetch_errors = []
    cmoney_results, as_of, cached = get_warrant_results(
        stock_codes, force=True, errors_out=fetch_errors
    )
    meta = {
        "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).isoformat() if as_of else None,
        "cached": cached,
        # Set only when CMoney itself failed — an empty result with no
        # hard_error means "this underlying has no warrants", not an outage.
        "hard_error": "; ".join(fetch_errors) if fetch_errors else None,
    }

    codes_s = ",".join(stock_codes)
    if not cmoney_results:
        if fetch_errors:
            err = "CMoney fetch failed: " + "; ".join(fetch_errors)
            applog.log("WARR", f"{codes_s} -> 0 rows ({err})", level="ERROR")
            return pd.DataFrame(), err, meta
        applog.log("WARR", f"{codes_s} -> 0 rows (no warrants found)")
        return pd.DataFrame(), "No warrants found", meta

    df = build_warrant_df(
        cmoney_results, compute_iv=compute_iv, keep_noniv=keep_noniv,
        allow_no_quote=allow_no_quote,
    )

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
        allow_no_quote=allow_no_quote,
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


def fetch_iv_surface(stock_codes, option_type="All", dte_min=None, dte_max=None,
                     strike_min=None, strike_max=None):
    """Warrants for the IV surface, with the filters that apply to BOTH quote sides.

    The IV bounds are deliberately not applied here: the bid and ask surfaces are
    built from their own IV columns and are filtered per side by the caller, so a
    warrant can appear on one sheet and not the other. `None` means unbounded.
    """
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

    keep = (
        (df["days_to_expiry"] > 0)
        & (df["days_to_expiry"] < 666)
        # A bid IV a full 100 vol points from the ask is a solver artefact, not a
        # spread — drop the row from both sides rather than letting it tilt either.
        & (abs(df["iv_ask"] - df["iv_bid"]) < 1)
    )
    if dte_min is not None:
        keep &= df["days_to_expiry"] >= dte_min
    if dte_max is not None:
        keep &= df["days_to_expiry"] <= dte_max
    if strike_min is not None:
        keep &= df["strike"] >= strike_min
    if strike_max is not None:
        keep &= df["strike"] <= strike_max

    df_clean = df[keep].copy()
    if df_clean.empty:
        return None, "No warrants passed the surface filters", meta

    return df_clean, None, meta
