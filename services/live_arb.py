"""Live Arb tab session: TSMC-only, reruns Direct Match against the live
websocket quotes services/live_warrant.py and services/live_options.py
already maintain, instead of the batch CMoney/TAIFEX snapshot arb_logic.py
normally matches against.

Deliberately does NOT start or own either of those two sessions — Live Arb
only reads whatever the Live Warrant / Live Options tabs already have
tracked (manual pre-load, by design: this tab must never be the thing that
silently opens a Fubon connection). If neither tab has TSMC loaded yet,
scanning just produces zero active hits until they do.

A background thread (`_scan_loop`), started/stopped by `start_scan()`/
`stop_scan()` — the tab's kill switch, independent of the other two tabs'
own connect/disconnect — polls a short fixed interval and only redoes the
actual matcher call when the combined tick-seq from both live caches has
moved since the last scan (the dirty-gate: see live_warrant.py's `_book_seq`
and live_options.py's `_tick_seq`). This runs faster than the browser's
500ms poll of `get_data()` on purpose: ticks arrive every ~250-330ms, so
detection has to run independently of the UI refresh rate or a transient
arb could open and close between two polls and never get logged (see the
plan this shipped under).
"""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from logic import live_arb_logic
from services import db_live_arb, live_options, live_warrant

TW_TZ = ZoneInfo("Asia/Taipei")
UNDERLYING = "2330"

# Sleep between dirty-gate checks. Well under the ~250-330ms tick spacing a
# 3-4 ticks/sec feed implies, and each actual scan is ~1-50ms of work (see
# the plan's benchmark), so polling this often costs nothing when idle and
# still reacts within roughly one tick's latency when something changes.
POLL_INTERVAL_S = 0.15

_lock = threading.RLock()
_enabled = False
_thread = None
_stop_event = threading.Event()

_last_seq = None
_active_hits = []
_logged_today = set()   # dedup keys already inserted today (seeded from Supabase)
_trade_date = None      # the TW date _logged_today was seeded for
_session_error = None


def _today():
    return datetime.now(TW_TZ).date()


def _seed_logged_today():
    """Load today's already-logged dedup keys from Supabase so a mid-day
    restart of this process doesn't re-log a pair that already fired."""
    global _logged_today, _trade_date, _session_error
    today = _today()
    try:
        ids = db_live_arb.existing_ids_for_date(today)
    except Exception as e:
        _session_error = f"failed to seed today's logged trades: {type(e).__name__}: {e}"
        print(f"LIVEARB: {_session_error}", flush=True)
        ids = set()
    with _lock:
        _logged_today = ids
        _trade_date = today


def _combined_seq():
    w_seq, warrant_rows = live_warrant.snapshot_for_underlying(UNDERLYING)
    o_seq, option_rows = live_options.snapshot_for_underlying(UNDERLYING)
    return w_seq + o_seq, warrant_rows, option_rows


def _log_new_hits(hits, today):
    """Best-leg-per-warrant, then insert whatever isn't already logged
    today. One Supabase call per new hit — the number of genuinely NEW
    unique pairs in a day is small, so this is never a hot path."""
    global _logged_today
    for row in live_arb_logic.best_per_warrant(hits):
        key = live_arb_logic.dedup_key(row, today)
        with _lock:
            already = key in _logged_today
        if already:
            continue
        payload = {
            "id": key,
            "trade_date": today.isoformat(),
            "warrant_code": row["warrant_code"],
            "warrant_name": row["warrant_name"],
            "option_contract": row["option_code"],
            "price_diff": row["price_diff"],
            "price_diff_pct": row["price_diff_pct"],
            "warrant_ask": row["warrant_ask"],
            "opt_bid": row["opt_bid"],
        }
        try:
            db_live_arb.insert_trade(payload)
        except Exception as e:
            # A duplicate-key race (two ticks both saw it "new" before either
            # committed) or a transient Supabase error either way — don't let
            # a logging failure kill the scan loop, and don't mark it seen so
            # a genuine transient failure gets retried on the next new hit
            # for this pair (a duplicate-key race, on the other hand, means
            # it's already logged, which is the outcome we wanted anyway).
            print(f"LIVEARB: log trade {key} failed: {type(e).__name__}: {e}", flush=True)
            continue
        with _lock:
            _logged_today.add(key)


def _scan_once():
    global _active_hits, _last_seq
    today = _today()
    with _lock:
        needs_reseed = _trade_date != today
    if needs_reseed:
        _seed_logged_today()  # a Supabase call — never made while holding _lock

    seq, warrant_rows, option_rows = _combined_seq()
    with _lock:
        unchanged = _last_seq is not None and seq == _last_seq
        _last_seq = seq
    if unchanged:
        return

    hits = live_arb_logic.scan(warrant_rows, option_rows, today)
    with _lock:
        _active_hits = hits
    _log_new_hits(hits, today)


def _scan_loop():
    print("LIVEARB: scan loop started", flush=True)
    while not _stop_event.is_set():
        try:
            _scan_once()
        except Exception as e:
            print(f"LIVEARB: scan tick failed: {type(e).__name__}: {e}", flush=True)
        _stop_event.wait(POLL_INTERVAL_S)
    print("LIVEARB: scan loop stopped", flush=True)


def start_scan():
    """The kill switch's "on". Idempotent — a no-op if already running."""
    global _enabled, _thread, _last_seq, _session_error
    with _lock:
        if _enabled:
            return
        _enabled = True
        _last_seq = None
    _session_error = None
    _seed_logged_today()
    _stop_event.clear()
    _thread = threading.Thread(target=_scan_loop, daemon=True)
    _thread.start()


def stop_scan():
    """The kill switch's "off". Signals the loop to exit; does not touch the
    Live Warrant / Live Options sessions at all."""
    global _enabled
    with _lock:
        if not _enabled:
            return
        _enabled = False
    _stop_event.set()


def get_data():
    """Snapshot for the /live_arb_data poll route."""
    with _lock:
        return {
            "enabled": _enabled,
            "session_error": _session_error,
            "active_hits": list(_active_hits),
            "active_count": len(_active_hits),
            "logged_count_today": len(_logged_today),
            "trade_date": _trade_date.isoformat() if _trade_date else None,
        }
