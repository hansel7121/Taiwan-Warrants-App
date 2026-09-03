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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from logic import iv_engine, live_arb_logic, live_arb_lp_logic
from services import db_live_arb, db_live_arb_lp, live_options, live_warrant

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


def _format_tick(tick):
    """JSON-able form of live_arb_logic.latest_tick()'s result — `ts` becomes
    a seconds-ago float computed right now, same convention as
    live_warrant.py's `_underlying_row_payload`/live_options.py's
    `_contract_payload` age fields. None stays None (no tick yet)."""
    if tick is None:
        return None
    return {
        "kind": tick["kind"], "code": tick["code"], "name": tick["name"],
        "bid": tick["bid"], "ask": tick["ask"],
        "seconds_ago": round((datetime.now(timezone.utc) - tick["ts"]).total_seconds(), 1),
    }


def _tick_and_freshness(last_seq):
    """Shared by get_data()/get_lp_data(): re-fetches the two live caches
    fresh (never the scan loop's own possibly-stale `_last_seq`/
    `_lp_last_seq` snapshot) and reports both what the most recent tick
    looked like and whether `last_seq` (whichever loop's own bookkeeping
    the caller passes in) has caught up to it.

    This is what makes "Arb is/is not up to date" a real check rather than
    an assumption: `last_seq` only advances when that loop's own
    `_scan_once`/`_lp_scan_once` iteration runs, so if scanning is stopped
    (or a slow LP scan is still catching up on a burst of ticks), the seq
    computed here keeps moving while `last_seq` doesn't, and this reports
    the mismatch honestly instead of just assuming the background loop is
    keeping up.
    """
    seq, warrant_rows, option_rows = _combined_seq()
    tick = live_arb_logic.latest_tick(warrant_rows, option_rows)
    up_to_date = last_seq is not None and seq == last_seq
    return _format_tick(tick), up_to_date


def get_data():
    """Snapshot for the /live_arb_data poll route."""
    with _lock:
        last_seq = _last_seq
    last_tick, up_to_date = _tick_and_freshness(last_seq)
    with _lock:
        return {
            "enabled": _enabled,
            "session_error": _session_error,
            "active_hits": list(_active_hits),
            "active_count": len(_active_hits),
            "logged_count_today": len(_logged_today),
            "trade_date": _trade_date.isoformat() if _trade_date else None,
            "last_tick": last_tick,
            "up_to_date": up_to_date,
        }


# ─────────────────────────────────────────────────────────────────────────────
# LP subtab: static-arb LP against the same live TSMC quotes, via the
# Rust-only rust/warrants_core::solve_static_arb_horizon kernel (see
# logic/live_arb_lp_logic.py and logic/iv_engine.py — deliberately no Python
# fallback). Fully independent state/kill-switch from the Direct Match scan
# above: the newer, less battle-tested LP path can be stopped without
# touching the already-proven Direct Match one. A full LP scan is far more
# expensive (~90ms measured, vs Direct Match's ~8ms) and can't be
# tick-synchronous — see the plan this shipped under — so this loop is
# self-paced (start the next scan the instant the previous one finishes)
# rather than trying to match Direct Match's near-tick cadence; it still
# reuses the same dirty-gate signal (`_combined_seq`) so an idle period
# costs nothing.
# ─────────────────────────────────────────────────────────────────────────────
LP_POLL_INTERVAL_S = 0.1

_lp_lock = threading.RLock()
_lp_enabled = False
_lp_thread = None
_lp_stop_event = threading.Event()

_lp_last_seq = None
_lp_active_structures = []
_lp_logged_today = set()
_lp_trade_date = None
_lp_session_error = None


def _lp_seed_logged_today():
    global _lp_logged_today, _lp_trade_date, _lp_session_error
    today = _today()
    try:
        ids = db_live_arb_lp.existing_ids_for_date(today)
    except Exception as e:
        _lp_session_error = f"failed to seed today's logged LP trades: {type(e).__name__}: {e}"
        print(f"LIVEARB-LP: {_lp_session_error}", flush=True)
        ids = set()
    with _lp_lock:
        _lp_logged_today = ids
        _lp_trade_date = today


def _lp_log_new_structures(rows, today):
    global _lp_logged_today
    for row in rows:
        key = live_arb_lp_logic.dedup_key(row, today)
        with _lp_lock:
            already = key in _lp_logged_today
        if already:
            continue
        payload = {
            "id": key,
            "trade_date": today.isoformat(),
            "horizon_dte": row["horizon_dte"],
            "legs": row["legs"],
            "net_credit": row["net_credit"],
            "min_payoff": row["min_payoff"],
            "guaranteed_profit": row["guaranteed_profit"],
            "worst_spot": row["worst_spot"],
            "gross_debit": row["gross_debit"],
            "return_pct": row["return_pct"],
        }
        try:
            db_live_arb_lp.insert_trade(payload)
        except Exception as e:
            # Same reasoning as Direct Match's _log_new_hits: never let a
            # logging failure kill the scan loop, and don't mark it seen so
            # a transient failure gets retried on this structure's next scan.
            print(f"LIVEARB-LP: log structure {key} failed: {type(e).__name__}: {e}", flush=True)
            continue
        with _lp_lock:
            _lp_logged_today.add(key)


def _lp_scan_once():
    global _lp_active_structures, _lp_last_seq
    today = _today()
    with _lp_lock:
        needs_reseed = _lp_trade_date != today
    if needs_reseed:
        _lp_seed_logged_today()  # a Supabase call — never made while holding _lp_lock

    seq, warrant_rows, option_rows = _combined_seq()
    with _lp_lock:
        unchanged = _lp_last_seq is not None and seq == _lp_last_seq
        _lp_last_seq = seq
    if unchanged:
        return

    rows = live_arb_lp_logic.scan(warrant_rows, option_rows, today)
    with _lp_lock:
        _lp_active_structures = rows
    _lp_log_new_structures(rows, today)


def _lp_scan_loop():
    print("LIVEARB-LP: scan loop started", flush=True)
    while not _lp_stop_event.is_set():
        try:
            _lp_scan_once()
        except Exception as e:
            print(f"LIVEARB-LP: scan tick failed: {type(e).__name__}: {e}", flush=True)
            with _lp_lock:
                _lp_session_error = f"{type(e).__name__}: {e}"
        _lp_stop_event.wait(LP_POLL_INTERVAL_S)
    print("LIVEARB-LP: scan loop stopped", flush=True)


def start_lp_scan():
    """The LP subtab's own kill switch, independent of start_scan()/
    stop_scan() above. Idempotent — a no-op if already running. Requires the
    Rust engine (no Python fallback exists for this kernel); surfaces a
    clear session_error instead of starting a loop that could never
    succeed if it's missing."""
    global _lp_enabled, _lp_thread, _lp_last_seq, _lp_session_error
    with _lp_lock:
        if _lp_enabled:
            return
        _lp_enabled = True
        _lp_last_seq = None
    _lp_session_error = None
    if not iv_engine.RUST_AVAILABLE:
        with _lp_lock:
            _lp_session_error = "Rust engine not available — Live Arb LP requires it (no Python fallback)"
            _lp_enabled = False
        return
    _lp_seed_logged_today()
    _lp_stop_event.clear()
    _lp_thread = threading.Thread(target=_lp_scan_loop, daemon=True)
    _lp_thread.start()


def stop_lp_scan():
    """The LP subtab's kill switch's "off". Does not touch Direct Match's
    scan or either Live Warrant/Live Options session."""
    global _lp_enabled
    with _lp_lock:
        if not _lp_enabled:
            return
        _lp_enabled = False
    _lp_stop_event.set()


def get_lp_data():
    """Snapshot for the /live_arb_lp_data poll route."""
    with _lp_lock:
        last_seq = _lp_last_seq
    last_tick, up_to_date = _tick_and_freshness(last_seq)
    with _lp_lock:
        return {
            "enabled": _lp_enabled,
            "session_error": _lp_session_error,
            "active_structures": list(_lp_active_structures),
            "active_count": len(_lp_active_structures),
            "logged_count_today": len(_lp_logged_today),
            "trade_date": _lp_trade_date.isoformat() if _lp_trade_date else None,
            "last_tick": last_tick,
            "up_to_date": up_to_date,
        }
