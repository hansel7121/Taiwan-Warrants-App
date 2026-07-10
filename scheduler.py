"""Background market-data refresh.

Runs inside the (single) web process via APScheduler so the jobs share the
module-level caches in warrant_logic / options_logic / us_options_logic.
Started once from wsgi.py (production) or app.py __main__ (dev).
"""
import threading
import time
import traceback
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

import warrant_logic
import options_logic
import us_options_logic

# Mirror of DEFAULT_STOCKS in templates/index.html — keep the two in sync.
DEFAULT_WARRANT_STOCKS = [
    "2330", "2317", "2454", "2382", "3231", "6669", "2376", "3017", "3324",
    "2308", "3711", "3034", "2379", "3661", "3443", "2603", "3008", "2881",
    "2882", "3037", "2303", "2886",
]
TW_OPTION_CODES = list(options_logic.COMMODITY_MAP)
US_OPTION_CODES = list(us_options_logic.US_ADR_MAP)

REFRESH_MINUTES = 15
CMKEY_MINUTES = 45
FORCE_DEBOUNCE_SECONDS = 60

_scheduler = None
_start_lock = threading.Lock()
_last_run: dict = {}  # job name -> epoch of last completed run


def _custom_stock_codes():
    """Union of every user's custom-stock codes, from Supabase.

    Returns [] on any failure (incl. unconfigured Supabase) so the refresh
    jobs keep running against the default universe.
    """
    try:
        import db
        return db.all_custom_stock_codes()
    except Exception as e:
        print(f"SCHED: custom stock codes unavailable: {e}", flush=True)
        return []


def warrant_universe():
    return sorted(set(DEFAULT_WARRANT_STOCKS) | set(_custom_stock_codes()))


def _job(name, fn):
    """Run one refresh job with logging; never let an exception kill the scheduler."""
    t0 = time.time()
    try:
        fn()
        _last_run[name] = time.time()
        print(f"SCHED: {name} ok in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"SCHED: {name} FAILED after {time.time() - t0:.1f}s: {e}", flush=True)
        traceback.print_exc()


def refresh_cmkey():
    _job("cmkey", warrant_logic.refresh_cmoney_key)


def refresh_warrants():
    _job("warrants", lambda: warrant_logic.refresh_warrant_cache(warrant_universe()))


def refresh_tw_options():
    _job("tw_options", lambda: options_logic.refresh_cache(TW_OPTION_CODES))


def refresh_us_options():
    _job("us_options", lambda: us_options_logic.refresh_cache(US_OPTION_CODES))


_FORCE_MAP = {
    "warrants": refresh_warrants,
    "tw_options": refresh_tw_options,
    "us_options": refresh_us_options,
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
        # cmkey first: the warrant warm-up 60s later needs it.
        warrant_logic.prefetch_cmoney_key()
        sched = BackgroundScheduler(daemon=True)
        now = datetime.now()
        sched.add_job(refresh_cmkey, "interval", minutes=CMKEY_MINUTES)
        sched.add_job(refresh_warrants, "interval", minutes=REFRESH_MINUTES,
                      next_run_time=now + timedelta(seconds=60))
        sched.add_job(refresh_tw_options, "interval", minutes=REFRESH_MINUTES,
                      next_run_time=now + timedelta(seconds=5))
        sched.add_job(refresh_us_options, "interval", minutes=REFRESH_MINUTES,
                      next_run_time=now + timedelta(seconds=10))
        sched.start()
        _scheduler = sched
        print("SCHED: started", flush=True)
        return sched
