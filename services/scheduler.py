"""Background market-data refresh.

Runs inside the (single) web process via APScheduler so the jobs share the
module-level caches in warrant_logic / options_logic / us_options_logic.
Started once from wsgi.py (production) or app.py __main__ (dev).

Step 3 of the Supabase market-data migration: the refresh jobs now WRITE
Supabase snapshots (db_market.write_snapshot / set_key). The core writers are
plain functions (fetch + align columns + write) so they can be called directly
by force_refresh and the validation harness, bypassing the cron grid and the
market-hours gate. The scheduler registers them wrapped in _job (logging) and,
for the three intraday data jobs, _gated (skip when the relevant market is
closed) on a wall-clock 15-minute cron grid.
"""
import threading
import time
import traceback
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from services import memlog
from services import db_market
from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic

# Mirror of DEFAULT_STOCKS in templates/index.html — keep the two in sync.
DEFAULT_WARRANT_STOCKS = [
    "2330", "2317", "2454", "2382", "3231", "6669", "2376", "3017", "3324",
    "2308", "3711", "3034", "2379", "3661", "3443", "2603", "3008", "2881",
    "2882", "3037", "2303", "2886",
]
TW_OPTION_CODES = list(options_logic.COMMODITY_MAP)
US_OPTION_CODES = list(us_options_logic.US_ADR_MAP)

# Superset-with-IV column order the writers align each option leg to before the
# snapshot write. These MUST match the data columns (minus batch_id) of the
# md_tw_options / md_us_options tables in supabase/schema.sql. reindex drops any
# extra columns and fills missing ones with NaN (-> NULL on write).
TW_OPTION_COLS = [
    "stock_code", "source", "contract", "type", "underlying_price", "ask", "bid",
    "days_to_expiry", "strike", "exercise_ratio", "bid_size", "ask_size",
    "volume", "oi", "time_value_am", "iv_ask", "iv_bid", "delta_calc",
    "leverage_calc", "is_live", "quote_time",
]
US_OPTION_COLS = [
    "stock_code", "contract", "type", "underlying_price", "strike",
    "days_to_expiry", "bid", "ask", "iv_ask", "iv_bid", "delta_calc", "volume",
    "oi", "is_live", "strike_usd", "bid_usd", "ask_usd", "adr_price", "fx",
]

# Columns typed `integer` in supabase/schema.sql. keep_noniv=True leaves NaNs in
# the frame, which makes these columns float64 (1.0, NaN); Postgres rejects
# "1.0" for an integer column, so coerce them to pandas nullable Int64 (whole
# ints, NaN -> <NA> -> NULL) before the write.
TW_OPTION_INT_COLS = ["days_to_expiry", "bid_size", "ask_size", "volume", "oi"]
US_OPTION_INT_COLS = ["days_to_expiry", "volume", "oi"]

REFRESH_MINUTES = 15
CMKEY_MINUTES = 45
UNIVERSE_HOURS = 24
FORCE_DEBOUNCE_SECONDS = 60

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_TZ_NY = ZoneInfo("America/New_York")

_scheduler = None
_start_lock = threading.Lock()
_last_run: dict = {}  # job name -> epoch of last completed run


def _coerce_int_cols(df, cols):
    """Cast schema-integer columns to pandas nullable Int64 (whole ints / <NA>).

    Postgres integer columns reject float text like "1.0"; keep_noniv NaNs force
    these count columns to float. Round-then-Int64 gives clean ints and keeps
    missing values null.
    """
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round().astype("Int64")
    return df


def _custom_stock_codes():
    """Union of every user's custom-stock codes, from Supabase.

    Returns [] on any failure (incl. unconfigured Supabase) so the refresh
    jobs keep running against the default universe.
    """
    try:
        from services import db
        return db.all_custom_stock_codes()
    except Exception as e:
        print(f"SCHED: custom stock codes unavailable: {e}", flush=True)
        return []


def warrant_universe():
    return sorted(set(DEFAULT_WARRANT_STOCKS) | set(_custom_stock_codes()))


# ---------------------------------------------------------------------------
# Market-hours gate
# ---------------------------------------------------------------------------
def _localize(now, tz):
    """Return a tz-aware datetime in `tz`.

    now=None -> current time in tz. A naive `now` is interpreted as wall-clock
    time already in `tz` (handy for tests). A tz-aware `now` is converted.
    """
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def is_market_open(market, now=None):
    """Whether `market` is in a trading window at `now` (default: right now).

    Holidays are intentionally ignored in v1 — only weekday + time-of-day.
    weekday: Mon=0 .. Sun=6. `now` is injectable for deterministic testing.
    """
    if market == "tw_equity":
        t = _localize(now, _TZ_TAIPEI)
        return t.weekday() <= 4 and dtime(9, 0) <= t.time() <= dtime(13, 30)

    if market == "tw_option":
        t = _localize(now, _TZ_TAIPEI)
        wd = t.weekday()
        tt = t.time()
        day = wd <= 4 and dtime(8, 45) <= tt <= dtime(13, 45)
        # Night session runs Mon–Fri 15:00 -> next-day 05:00; the pre-05:00
        # morning therefore belongs to Tue–Sat (weekday 1..5).
        night = (wd <= 4 and tt >= dtime(15, 0)) or (1 <= wd <= 5 and tt < dtime(5, 0))
        return day or night

    if market == "us_option":
        t = _localize(now, _TZ_NY)
        # ZoneInfo handles US DST automatically, so 09:30–16:00 ET is correct
        # year-round.
        return t.weekday() <= 4 and dtime(9, 30) <= t.time() <= dtime(16, 0)

    raise ValueError(f"unknown market: {market}")


# ---------------------------------------------------------------------------
# Core writers — plain fetch + align + write_snapshot. No gate, no _job wrapper;
# callable directly by force_refresh and the validation harness.
# ---------------------------------------------------------------------------
def refresh_cmkey():
    """Fetch the CMoney key and persist it to Supabase (cmoney_key store)."""
    key = warrant_logic.refresh_cmoney_key()
    if not key:
        # refresh_cmoney_key returns the fetched string, but fall back to the
        # accessor in case a future refactor changes its return.
        key = warrant_logic.get_cmoney_key()
    if key:
        db_market.set_key(key)
        print("SCHED: cmkey persisted", flush=True)
    else:
        print("SCHED: cmkey empty, not persisted", flush=True)


def refresh_universe():
    """Re-scrape the ISIN listing (in-memory swap) then snapshot the universe."""
    warrant_logic.refresh_warrant_universe()
    rows = warrant_logic.universe_rows()
    if not rows:
        print("SCHED: universe empty, skipping write", flush=True)
        return
    df = pd.DataFrame(rows)
    db_market.write_snapshot("warrant_universe", df)


def refresh_warrants():
    """Fetch the full warrant superset (IV computed, non-converged kept) + write."""
    df, err, meta = warrant_logic.fetch_warrants_live(
        warrant_universe(), "All", 0, 365, 0.0, 1e9, 0,
        compute_iv=True, keep_noniv=True,
    )
    if df is None or df.empty:
        print(f"SCHED: warrants empty ({err}), skipping write", flush=True)
        return
    db_market.write_snapshot("warrants", df)


def refresh_tw_options():
    """Fetch each TW option product (superset-with-IV), align, write one batch."""
    frames = []
    for code in TW_OPTION_CODES:
        try:
            df = options_logic.fetch_options_live(
                [code], "All", min_days=0, max_days=365,
                compute_iv=True, keep_noniv=True,
            )
        except Exception as e:
            # One dead product must not sink the batch.
            print(f"SCHED: tw_option {code} failed: {e}", flush=True)
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df["stock_code"] = code
        # Provenance is informational and nullable; leaving it null is the
        # accepted Step-3 scope (we don't infer mis/eod here).
        df["source"] = None
        frames.append(df)
    if not frames:
        print("SCHED: tw_options empty, skipping write", flush=True)
        return
    out = pd.concat(frames, ignore_index=True).reindex(columns=TW_OPTION_COLS)
    out = _coerce_int_cols(out, TW_OPTION_INT_COLS)
    db_market.write_snapshot("tw_options", out)


def refresh_us_options():
    """Fetch each US ADR option chain (superset-with-IV), align, write one batch."""
    frames = []
    for code in US_OPTION_CODES:
        try:
            df = us_options_logic.fetch_us_options_live(
                code, "All", min_days=1, max_days=730,
                compute_iv=True, keep_noniv=True,
            )
        except Exception as e:
            print(f"SCHED: us_option {code} failed: {e}", flush=True)
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df["stock_code"] = code
        frames.append(df)
    if not frames:
        print("SCHED: us_options empty, skipping write", flush=True)
        return
    out = pd.concat(frames, ignore_index=True).reindex(columns=US_OPTION_COLS)
    out = _coerce_int_cols(out, US_OPTION_INT_COLS)
    db_market.write_snapshot("us_options", out)


# ---------------------------------------------------------------------------
# Job / gate wrappers used at registration time
# ---------------------------------------------------------------------------
def _job(name, fn):
    """Run one refresh job with logging; never let an exception kill the scheduler."""
    t0 = time.time()
    try:
        with memlog.measure(name):
            fn()
        _last_run[name] = time.time()
        print(f"SCHED: {name} ok in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"SCHED: {name} FAILED after {time.time() - t0:.1f}s: {e}", flush=True)
        traceback.print_exc()


def _gated(market, fn):
    """Wrap fn so it only runs when `market` is open (checked at run time)."""
    def run():
        if not is_market_open(market):
            print(f"SCHED: {market} closed, skipping", flush=True)
            return
        fn()
    return run


# Registration targets: each core writer wrapped in _job logging. These (not the
# bare cores) are what the scheduler and force_refresh dispatch.
def _run_cmkey():
    _job("cmkey", refresh_cmkey)


def _run_universe():
    _job("universe", refresh_universe)


def _run_warrants():
    _job("warrants", refresh_warrants)


def _run_tw_options():
    _job("tw_options", refresh_tw_options)


def _run_us_options():
    _job("us_options", refresh_us_options)


# No "universe" entry: /refresh is a refresh-prices-now action, and force_refresh
# ("all") would fire a multi-minute ISIN scrape on every click. The daily job and
# warrant_logic._ensure_universe_fetch already keep it current. These call the
# ungated core writers (via _job) so a manual /refresh works regardless of hours.
_FORCE_MAP = {
    "warrants": _run_warrants,
    "tw_options": _run_tw_options,
    "us_options": _run_us_options,
}


def force_refresh(kind="all"):
    """Manual refresh for the /refresh route; debounced per kind."""
    kinds = list(_FORCE_MAP) if kind == "all" else [kind]
    unknown = [k for k in kinds if k not in _FORCE_MAP]
    if unknown:
        return {"ok": False, "error": f"unknown kind: {unknown[0]}"}
    now = time.time()
    started, skipped = [], []
    for k in kinds:
        if now - _last_run.get(k, 0) < FORCE_DEBOUNCE_SECONDS:
            skipped.append(k)
            continue
        # Mark at dispatch, not completion, so a second click while the
        # refresh is still running debounces instead of double-fetching.
        _last_run[k] = now
        threading.Thread(target=_FORCE_MAP[k], daemon=True).start()
        started.append(k)
    return {"ok": True, "started": started, "skipped": skipped}


def last_run(name):
    ts = _last_run.get(name)
    return datetime.fromtimestamp(ts).isoformat() if ts else None


def start():
    """Start the scheduler exactly once per process."""
    global _scheduler
    with _start_lock:
        if _scheduler is not None:
            return _scheduler
        # Single-threaded executor: with max_workers=1 no two pandas-heavy
        # refresh jobs can overlap, which is what keeps the process under
        # Render's 512 MB cap.
        sched = BackgroundScheduler(
            daemon=True,
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={"max_instances": 1, "coalesce": True},
        )
        now = datetime.now()
        # Boot order: cmkey scrape first, since every warrant fetch needs it.
        # The single-worker executor serialises everything, so these staggered
        # next_run_time offsets only decide the order jobs are queued at boot.
        #
        # cmkey stays UNGATED on its own interval: a valid key must exist before
        # the 09:00 open and it is cheap. universe stays UNGATED on its daily
        # interval. The three intraday data jobs move to a wall-clock 15-minute
        # CRON grid, each gated to its market's trading hours.
        sched.add_job(_run_cmkey, "interval", minutes=CMKEY_MINUTES,
                      next_run_time=now + timedelta(seconds=1))
        # Universe before warrants: the single-worker executor serialises them, so
        # the first warrant refresh resolves against the fresh ISIN listing rather
        # than the stale bundled snapshot (it waits on the multi-minute scrape;
        # requests meanwhile fetch-on-miss off the fallback).
        sched.add_job(_run_universe, "interval", hours=UNIVERSE_HOURS,
                      next_run_time=now + timedelta(seconds=5))
        sched.add_job(_gated("tw_equity", _run_warrants),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("tw_option", _run_tw_options),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("us_option", _run_us_options),
                      CronTrigger(minute="0,15,30,45"))
        sched.start()
        _scheduler = sched
        print("SCHED: started", flush=True)
        return sched
