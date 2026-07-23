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
from logic import arb_logic
from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic

# Reuse the Find Arb tab's own defaults (templates/index.html #arbMaxStrikePct
# / #arbMaxDteDiff), same values app.py's /match_warrant_tw_option route falls
# back to — the suggest job should surface exactly what a user would see
# clicking "Warrant vs TW Option" with the page's own defaults, not an
# invented threshold.
SUGGEST_MAX_STRIKE_DIFF_PCT = 3.0
SUGGEST_MAX_DTE_DIFF = 5

# Superset-with-IV column order the writers align each option leg to before the
# snapshot write. These MUST match the data columns (minus batch_id) of the
# md_tw_options / md_us_options tables in supabase/schema.sql. reindex drops any
# extra columns and fills missing ones with NaN (-> NULL on write).
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

# Columns typed `integer` in supabase/schema.sql. keep_noniv=True leaves NaNs in
# the frame, which makes these columns float64 (1.0, NaN); Postgres rejects
# "1.0" for an integer column, so coerce them to pandas nullable Int64 (whole
# ints, NaN -> <NA> -> NULL) before the write.
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
def sync_cmkey():
    """Fetch the CMoney key and persist it to Supabase (cmoney_key store)."""
    key = warrant_logic.scrape_cmoney_key()
    if not key:
        # scrape_cmoney_key returns the fetched string, but fall back to the
        # accessor in case a future refactor changes its return.
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
    """Fetch the full warrant superset (IV computed, non-converged kept) + write."""
    df, err, meta = warrant_logic.scrape_cmoney_warrant(
        warrant_universe(), "All", 0, 365, 0.0, 1e9, 0,
        compute_iv=True, keep_noniv=True,
    )
    if df is None or df.empty:
        print(f"SCHED: warrants empty ({err}), skipping write", flush=True)
        return
    db_market.write_snapshot("warrants", df)


def sync_tw_option():
    """Fetch each TW option product (superset-with-IV), align, write one batch."""
    frames = []
    for code in tw_option_codes():
        try:
            df, err, _meta = options_logic.scrape_tw_option(
                [code], "All", min_days=0, max_days=365,
                compute_iv=True, keep_noniv=True,
            )
        except Exception as e:
            # One dead product must not sink the batch.
            print(f"SCHED: tw_option {code} failed: {e}", flush=True)
            continue
        if df is None or df.empty:
            if err:
                print(f"SCHED: tw_option {code} empty ({err})", flush=True)
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


def sync_us_option():
    """Fetch each US ADR option chain (superset-with-IV), align, write one batch."""
    frames = []
    for code in us_option_codes():
        try:
            df, err, _meta = us_options_logic.scrape_yfinance_us_option(
                code, "All", min_days=1, max_days=730,
                compute_iv=True, keep_noniv=True,
            )
        except Exception as e:
            print(f"SCHED: us_option {code} failed: {e}", flush=True)
            continue
        if df is None or df.empty:
            if err:
                print(f"SCHED: us_option {code} empty ({err})", flush=True)
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


# Direct tab's two strategies -> arb_suggestions arb_type tags, matching the
# exact mode strings static/js/arb.js already passes to openArbModal(row, mode)
# at the Find Arb tab (openArbModal(row, "direct") / (row, "pcp")) so the
# Suggestions sub-tab can route a stored row into that same modal unchanged.
_SUGGEST_STRATEGIES = (("direct_same_type", "same_type"), ("direct_pcp", "pcp"))


def sync_suggestions():
    """Scan the Direct tab's two strategies, persist positive-edge rows as suggestions.

    Only match_warrant_tw_option (not the US-option or TW/US cross-market
    matchers) — both legs must be TW-listed so the suggest job's own
    tw_equity gate (services/scheduler.py:start) actually bounds when a
    suggested trade is executable. Every row _match_warrants_to_options /
    _match_warrants_pcp returns is already profitable per its own
    direction-specific sign convention (that's what "match" means for these
    functions — same as what the Find Arb tab itself displays); the PCP
    matcher additionally emits NON-EXECUTABLE debug rows (short-warrant side,
    executable=False) that must be dropped here since warrants can't be
    shorted.
    """
    codes = sorted(set(warrant_universe()) & set(tw_option_codes()))
    if not codes:
        print("SCHED: suggestions: no warrant/tw_option overlap, skipping", flush=True)
        return

    for arb_type, strategy in _SUGGEST_STRATEGIES:
        try:
            df = arb_logic.match_warrant_tw_option(
                codes, "All", SUGGEST_MAX_STRIKE_DIFF_PCT, SUGGEST_MAX_DTE_DIFF,
                positive_loose=False, min_volume=0, strategy=strategy,
            )
        except RuntimeError as e:
            print(f"SCHED: suggestions {arb_type} no matches: {e}", flush=True)
            db_suggestions.mark_stale(arb_type, [])
            continue

        if strategy == "pcp" and "executable" in df.columns:
            df = df[df["executable"]]
        elif strategy == "same_type" and {"trade", "riskless"} <= set(df.columns):
            # Risk-free only. match_warrant_tw_option still emits (a) the
            # non-executable short-warrant direction and (b) the UNFAVORABLE
            # capped-loss side, which its strike gate admits within
            # max_strike_diff_pct (arb_logic.py: strike_ok = favorable | within-cap).
            # A suggestion must be a true arb, so keep only the long-warrant /
            # short-option side on the FAVORABLE strike: residual vertical pays
            # >= 0 at every spot (option strike above the warrant's for calls /
            # below for puts), priced richer per share (price_diff>0) and shorter
            # dated (opt_dte <= warrant_dte, already enforced for this direction).
            df = df[(df["trade"] == "Buy Warrant / Sell Option") & df["riskless"]]
        if df.empty:
            print(f"SCHED: suggestions {arb_type} 0 active (no executable rows)", flush=True)
            db_suggestions.mark_stale(arb_type, [])
            continue

        # Each (warrant_code, option_contract) pair yields at most one row per
        # arb_type: same_type dedupes pairs across its two directions
        # (arb_logic.py's `seen` set) and pcp keeps only the single executable
        # side per pair — so this pair is already a stable, deterministic
        # opportunity identity; no need to also encode strike/expiry/direction.
        recs = json.loads(df.to_json(orient="records"))
        rows = []
        touched_ids = []
        for row in recs:
            sug_id = f"{arb_type}:{row['warrant_code']}:{row['option_contract']}"
            touched_ids.append(sug_id)
            rows.append({
                "id": sug_id,
                "arb_type": arb_type,
                "legs": row,
                "price_diff": row["price_diff"],
                "price_diff_pct": row.get("price_diff_pct"),
                # The suggest job itself only runs while tw_equity is open
                # (see the "tw_equity" gate at registration below), and
                # opt_df was already filtered to is_live rows before either
                # matcher ran — so both legs are live for every row this job
                # ever produces. Kept as a field (not hardcoded away) since a
                # future relaxation of the job's own gate should make this
                # meaningful again rather than requiring a schema change.
                "legs_status": {"warrant": "live", "option": "live"},
            })
        db_suggestions.upsert_suggestions(rows)
        db_suggestions.mark_stale(arb_type, touched_ids)
        print(f"SCHED: suggestions {arb_type} {len(rows)} active", flush=True)


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


# "universe" is deliberately separate from the three price kinds above: it's a
# multi-minute ISIN scrape, so it's only reachable via its own /sync_universe
# route (never bundled into a combined "refresh everything" action). It still
# goes through the same debounced force_refresh dispatch as the others.
_FORCE_MAP = {
    "warrants": _run_warrants,
    "tw_options": _run_tw_options,
    "us_options": _run_us_options,
    "universe": _run_universe,
}


def force_refresh(kind):
    """Manual sync for the /sync_X routes; debounced per kind.

    Runs synchronously — blocks until the scrape+store completes — so the
    caller's response IS the completion signal (no polling needed). A
    debounced call still returns immediately, before any blocking work.
    """
    if kind not in _FORCE_MAP:
        return {"ok": False, "error": f"unknown kind: {kind}"}
    # The check-then-set below must be atomic: two near-simultaneous requests
    # for the same kind could otherwise both read a stale _last_run before
    # either writes it, both slip past the debounce, and both run the scraper
    # concurrently — racing each other's write_snapshot() for the same
    # category. The lock only guards this tiny check+mark; the actual
    # scrape+store runs outside it so different kinds (or the next window's
    # same kind) are never serialized by it.
    with _force_refresh_lock:
        now = time.time()
        if now - _last_run.get(kind, 0) < FORCE_DEBOUNCE_SECONDS:
            return {"ok": True, "started": [], "skipped": [kind]}
        # Mark at dispatch, not completion, so a concurrent click debounces
        # instead of double-fetching.
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
        # Single-threaded executor: with max_workers=1 no two pandas-heavy
        # refresh jobs can overlap, which is what keeps the process under
        # Render's 512 MB cap. That same single worker means jobs due at the
        # same cron tick queue up behind one another; APScheduler's default
        # misfire_grace_time (1s) is far shorter than a scrape can take, so a
        # job second (or third) in the queue gets silently dropped as
        # "missed" before its turn ever comes up rather than actually
        # running late. A generous grace window (10 min — comfortably longer
        # than any single job observed in MEM: logs) lets queued jobs still
        # fire instead of vanishing.
        sched = BackgroundScheduler(
            daemon=True,
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
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
        #
        # Fixed daily 07:00 TPE (before TWSE's 09:00 open) rather than an
        # interval-since-boot: an interval trigger's wall-clock time drifts with
        # every process restart (Render redeploy, OOM restart, local dev
        # restart), so the listing could go stale for a day if a restart lands
        # right after that day's already-completed run.
        sched.add_job(_run_universe, CronTrigger(hour=7, minute=0, timezone=_TZ_TAIPEI))
        sched.add_job(_gated("tw_equity", _run_warrants),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("tw_option", _run_tw_options),
                      CronTrigger(minute="0,15,30,45"))
        sched.add_job(_gated("us_option", _run_us_options),
                      CronTrigger(minute="0,15,30,45"))
        # Suggest job offset a couple minutes after the sync grid, not the same
        # tick: APScheduler doesn't guarantee execution order between
        # independently-registered jobs due at the same instant even under a
        # single-worker executor, so an explicit gap is what actually
        # guarantees sync_warrant/sync_tw_option have finished writing before
        # this job reads their snapshots. Gated on tw_equity (not tw_option,
        # which the warrant/option syncs use) — the binding constraint here is
        # executability: the warrant leg only trades 09:00-13:30 TPE, a
        # narrower window than TAIFEX's, so a suggestion is useless outside it
        # even though the option leg itself might still be live.
        sched.add_job(_gated("tw_equity", _run_suggestions),
                      CronTrigger(minute="2,17,32,47", timezone=_TZ_TAIPEI))
        sched.start()
        _scheduler = sched
        print("SCHED: started", flush=True)
        return sched
