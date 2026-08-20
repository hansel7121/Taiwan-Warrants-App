"""In-process APScheduler jobs that refresh market-data snapshots and the Direct-Arb suggestions log."""
import json
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
from services import db_products
from services import db_suggestions
from services import live_warrant
from logic import arb_logic
from logic import ttl_cache
from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic

# Matches the Find Arb tab's own defaults (#arbMaxStrikePct / #arbMaxDteDiff).
SUGGEST_MAX_STRIKE_DIFF_PCT = 3.0
SUGGEST_MAX_DTE_DIFF = 5

# Must match md_tw_options / md_us_options columns (minus batch_id) in supabase/schema.sql.
TW_OPTION_COLS = [
    "stock_code", "source", "contract", "type", "underlying_price", "ask", "bid",
    "days_to_expiry", "strike", "exercise_ratio", "bid_size", "ask_size",
    "volume", "oi", "time_value_am", "iv_ask", "iv_bid", "delta_calc",
    "leverage_calc", "is_live", "ask_live", "bid_live", "quote_time",
]
US_OPTION_COLS = [
    "stock_code", "contract", "type", "underlying_price", "strike",
    "days_to_expiry", "bid", "ask", "iv_ask", "iv_bid", "delta_calc", "volume",
    "oi", "is_live", "ask_live", "bid_live", "strike_usd", "bid_usd", "ask_usd",
    "adr_price", "fx",
]

# Columns typed `integer` in supabase/schema.sql; coerced to nullable Int64 before write.
TW_OPTION_INT_COLS = ["days_to_expiry", "bid_size", "ask_size", "volume", "oi"]
US_OPTION_INT_COLS = ["days_to_expiry", "volume", "oi"]

REFRESH_MINUTES = 15
CMKEY_MINUTES = 45
FORCE_DEBOUNCE_SECONDS = 60

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_TZ_NY = ZoneInfo("America/New_York")

_scheduler = None
_start_lock = threading.Lock()
_force_refresh_lock = threading.Lock()  # guards the force_refresh debounce check+set
_sync_lock = threading.Lock()  # serializes ALL sync work (scheduled + manual); see _job
_last_run: dict = {}  # job name -> epoch of last completed run


def _coerce_int_cols(df, cols):
    """Cast schema-integer columns to pandas nullable Int64 (whole ints / <NA>)."""
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round().astype("Int64")
    return df


def warrant_universe():
    return [row["code"] for row in db_products.list_warrant_stocks()]


def tw_option_codes():
    return [row["code"] for row in db_products.list_tw_option_products()]


def us_option_codes():
    return [row["code"] for row in db_products.list_us_option_products()]


# ---------------------------------------------------------------------------
# Market-hours gate
# ---------------------------------------------------------------------------
def _localize(now, tz):
    """Return a tz-aware datetime in `tz`; None -> now, naive -> assumed already in `tz`."""
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def is_market_open(market, now=None):
    """Whether `market` is in a trading window at `now` (weekday + time-of-day only, no holidays)."""
    if market == "tw_equity":
        t = _localize(now, _TZ_TAIPEI)
        return t.weekday() <= 4 and dtime(9, 0) <= t.time() <= dtime(13, 30)

    if market == "tw_option":
        t = _localize(now, _TZ_TAIPEI)
        wd = t.weekday()
        tt = t.time()
        day = wd <= 4 and dtime(8, 45) <= tt <= dtime(13, 45)
        # Night session runs Mon-Fri 15:00 -> next-day 05:00, so pre-05:00 belongs to Tue-Sat.
        night = (wd <= 4 and tt >= dtime(15, 0)) or (1 <= wd <= 5 and tt < dtime(5, 0))
        return day or night

    if market == "us_option":
        t = _localize(now, _TZ_NY)
        return t.weekday() <= 4 and dtime(9, 30) <= t.time() <= dtime(16, 0)

    raise ValueError(f"unknown market: {market}")


# ---------------------------------------------------------------------------
# Core writers — plain fetch + align + write_snapshot. No gate, no _job wrapper;
# callable directly by force_refresh and the validation harness.
# ---------------------------------------------------------------------------
def sync_cmkey():
    """Fetch the CMoney key and persist it to Supabase (cmoney_key store)."""
    key = warrant_logic.scrape_cmoney_key()
    if not key:
        key = warrant_logic.get_cmoney_key()
    if key:
        db_market.set_key(key)
        print("SCHED: cmkey persisted", flush=True)
    else:
        print("SCHED: cmkey empty, not persisted", flush=True)


def sync_universe():
    """Re-scrape the ISIN listing (in-memory swap) then snapshot the universe."""
    warrant_logic.scrape_twse_universe()
    rows = warrant_logic.universe_rows()
    if not rows:
        print("SCHED: universe empty, skipping write", flush=True)
        return
    df = pd.DataFrame(rows)
    db_market.write_snapshot("warrant_universe", df)


def sync_warrant():
    """Fetch the full warrant superset (IV computed, non-converged and unquoted kept) + write."""
    df, err, meta = warrant_logic.scrape_cmoney_warrant(
        warrant_universe(), "All", 0, 365, 0.0, 1e9, 0,
        compute_iv=True, keep_noniv=True, allow_no_quote=True,
    )
    if df is None or df.empty:
        print(f"SCHED: warrants empty ({err}), skipping write", flush=True)
        return
    db_market.write_snapshot("warrants", df)


def _sync_option_products(codes, scraper, cols, int_cols, *, label,
                          set_source=False):
    """Fetch one option product per code, tag + align them, return one frame (shared by tw/us syncs)."""
    frames = []
    for code in codes:
        try:
            df, err, _meta = scraper(code)
        except Exception as e:
            # One dead product must not sink the batch.
            print(f"SCHED: {label} {code} failed: {e}", flush=True)
            continue
        if df is None or df.empty:
            if err:
                print(f"SCHED: {label} {code} empty ({err})", flush=True)
            continue
        df = df.copy()
        df["stock_code"] = code
        if set_source:
            # Provenance is informational and nullable; not inferred (mis/eod) here.
            df["source"] = None
        frames.append(df)
    if not frames:
        print(f"SCHED: {label}s empty, skipping write", flush=True)
        return None
    out = pd.concat(frames, ignore_index=True).reindex(columns=cols)
    return _coerce_int_cols(out, int_cols)


def sync_tw_option():
    """Fetch each TW option product (superset-with-IV), align, write one batch."""
    out = _sync_option_products(
        tw_option_codes(),
        lambda code: options_logic.scrape_tw_option(
            [code], "All", min_days=0, max_days=365,
            compute_iv=True, keep_noniv=True,
        ),
        TW_OPTION_COLS, TW_OPTION_INT_COLS,
        label="tw_option", set_source=True,
    )
    if out is None:
        return
    db_market.write_snapshot("tw_options", out)


def sync_us_option():
    """Fetch each US ADR option chain (superset-with-IV), align, write one batch."""
    out = _sync_option_products(
        us_option_codes(),
        lambda code: us_options_logic.scrape_yfinance_us_option(
            code, "All", min_days=1, max_days=730,
            compute_iv=True, keep_noniv=True,
        ),
        US_OPTION_COLS, US_OPTION_INT_COLS,
        label="us_option",
    )
    if out is None:
        return
    db_market.write_snapshot("us_options", out)


# Matches the "direct" mode string static/js/arb.js passes to openArbModal, so
# stored rows route into the same modal. Only same_type (Call-Call/Put-Put) is scanned.
_SUGGEST_ARB_TYPE = "direct_same_type"


def sync_suggestions():
    """Append-only log of Arb Finder -> Direct Match (Call-Call/Put-Put) output, at the tab's own defaults."""
    codes = sorted(set(warrant_universe()) & set(tw_option_codes()))
    if not codes:
        print("SCHED: suggestions: no warrant/tw_option overlap, skipping", flush=True)
        return

    try:
        df = arb_logic.match_warrant_tw_option(
            codes, "All", SUGGEST_MAX_STRIKE_DIFF_PCT, SUGGEST_MAX_DTE_DIFF,
            positive_loose=False, min_volume=0, strategy="same_type",
        )
    except arb_logic.NoMatchesError as e:
        print(f"SCHED: suggestions no matches: {e}", flush=True)
        return

    # Direction is part of the id: a flipped long/short pair is a distinct trade.
    recs = json.loads(df.to_json(orient="records"))
    candidates = {}
    for row in recs:
        direction = "bw" if str(row.get("trade", "")).startswith("Buy Warrant") else "bo"
        sug_id = f"{_SUGGEST_ARB_TYPE}:{direction}:{row['warrant_code']}:{row['option_contract']}"
        candidates[sug_id] = {
            "id": sug_id,
            "arb_type": _SUGGEST_ARB_TYPE,
            "legs": row,
            "price_diff": row["price_diff"],
            "price_diff_pct": row.get("price_diff_pct"),
            "legs_status": {"warrant": "live", "option": "live"},
        }
    if not candidates:
        print("SCHED: suggestions no rows", flush=True)
        return

    # Insert only ids not already stored; existing rows' frozen prices are never touched.
    already = db_suggestions.existing_ids(list(candidates))
    new_rows = [r for cid, r in candidates.items() if cid not in already]
    if new_rows:
        db_suggestions.insert_suggestions(new_rows)
    print(
        f"SCHED: suggestions +{len(new_rows)} new, "
        f"{len(candidates) - len(new_rows)} already logged "
        f"({len(candidates)} found this scan)",
        flush=True,
    )


def sync_live_warrant():
    """Open the shared Fubon session if it isn't already (start_session is idempotent)."""
    live_warrant.start_session()


# ---------------------------------------------------------------------------
# Job / gate wrappers used at registration time
# ---------------------------------------------------------------------------
def _job(name, fn):
    """Run one refresh job with logging on _sync_lock; never let an exception kill the scheduler."""
    t0 = time.time()
    with _sync_lock:
        try:
            with memlog.measure(name):
                fn()
            _last_run[name] = time.time()
            print(f"SCHED: {name} ok in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"SCHED: {name} FAILED after {time.time() - t0:.1f}s: {e}", flush=True)
            traceback.print_exc()
        finally:
            _log_cache_stats(name)


def _log_cache_stats(name):
    """Log which in-process cache is holding memory after every job; never raises."""
    try:
        print(f"CACHE: after {name} | {ttl_cache.format_stats()}", flush=True)
    except Exception:
        pass


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
    _job("cmkey", sync_cmkey)


def _run_universe():
    _job("universe", sync_universe)


def _run_warrants():
    _job("warrants", sync_warrant)


def _run_tw_options():
    _job("tw_options", sync_tw_option)


def _run_us_options():
    _job("us_options", sync_us_option)


def _run_suggestions():
    _job("suggestions", sync_suggestions)


def _run_live_warrant():
    _job("live_warrant", sync_live_warrant)


_FORCE_MAP = {
    "warrants": _run_warrants,
    "tw_options": _run_tw_options,
    "us_options": _run_us_options,
    "universe": _run_universe,
}


def force_refresh(kind):
    """Manual sync for the /sync_X routes; runs synchronously, debounced per kind."""
    if kind not in _FORCE_MAP:
        return {"ok": False, "error": f"unknown kind: {kind}"}
    with _force_refresh_lock:
        now = time.time()
        if now - _last_run.get(kind, 0) < FORCE_DEBOUNCE_SECONDS:
            return {"ok": True, "started": [], "skipped": [kind]}
        _last_run[kind] = now
    _FORCE_MAP[kind]()
    return {"ok": True, "started": [kind], "skipped": []}


def last_run(name):
    ts = _last_run.get(name)
    return datetime.fromtimestamp(ts).isoformat() if ts else None


def start():
    """Start the scheduler exactly once per process."""
    global _scheduler
    with _start_lock:
        if _scheduler is not None:
            return _scheduler
        # Single-worker executor (Render's 512MB cap); generous misfire_grace_time
        # keeps queued jobs firing instead of being dropped as "missed".
        sched = BackgroundScheduler(
            daemon=True,
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
        )
        now = datetime.now()
        # cmkey and universe stay ungated on their own schedules; the three intraday
        # data jobs run on a wall-clock 15-min cron grid, gated to market hours.
        sched.add_job(_run_cmkey, "interval", minutes=CMKEY_MINUTES,
                      next_run_time=now + timedelta(seconds=1))
        # Fixed daily 07:00 TPE (before TWSE open) so a restart can't push it a day late.
        sched.add_job(_run_universe, CronTrigger(hour=7, minute=0, timezone=_TZ_TAIPEI))
        sched.add_job(_gated("tw_equity", _run_warrants),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("tw_option", _run_tw_options),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("us_option", _run_us_options),
                      CronTrigger(minute="0,15,30,45"))
        # Offset a couple minutes after the sync grid so warrant/option snapshots are
        # already written; gated on tw_equity since that's the narrower trading window.
        sched.add_job(_gated("tw_equity", _run_suggestions),
                      CronTrigger(minute="2,17,32,47", timezone=_TZ_TAIPEI))
        # Live Warrant's Fubon session: checked every 5 min (start_session is a
        # no-op once connected) and also attempted right away, so a restart
        # mid-trading-day doesn't wait for the next grid tick to reconnect.
        sched.add_job(_gated("tw_equity", _run_live_warrant),
                      CronTrigger(minute="*/5", timezone=_TZ_TAIPEI),
                      next_run_time=now + timedelta(seconds=2))
        sched.start()
        _scheduler = sched
        print("SCHED: started", flush=True)
        return sched
