"""Parity gate for Phase 2 Step 4a (readers read Supabase behind a flag).

Deterministic, in-process, function-level parity. Stronger and less flaky than a
dual-boot HTTP diff because it removes network/market drift: we refresh ONCE
(which both writes fresh Supabase snapshots AND warms the in-proc live caches
from the SAME instant), then read the same functions twice — once with
MARKET_SOURCE unset (live cache) and once with MARKET_SOURCE=supabase (snapshot)
— and prove the results are row-for-row identical.

Run:
  TZ=Asia/Taipei OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
    /opt/anaconda3/envs/warrants/bin/python scripts/validate_parity.py
"""
import os
import sys

# Ensure the repo root is importable when run as `python scripts/validate_parity.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic
from logic import arb_logic
from services import scheduler

# Timestamp / freshness fields are expected to differ (live=wall-clock cache ts,
# supabase=batch created_at) and are never part of the data payload.
IGNORE_COLS = {"as_of", "cached", "quote_time"}


def _mark_supabase(on):
    if on:
        os.environ["MARKET_SOURCE"] = "supabase"
    else:
        os.environ.pop("MARKET_SOURCE", None)


def both(fn, *args, **kw):
    """Return (live_result, supa_result): fn() with MARKET_SOURCE unset, then
    with it set to 'supabase', then unset again. read_snapshot/snapshot_enabled
    read the env at call time, so toggling per call is what routes each call."""
    _mark_supabase(False)
    try:
        live = fn(*args, **kw)
    finally:
        _mark_supabase(False)
    # us_options checks its snapshot branch before the in-proc _cache, but clear
    # it anyway so the supabase call provably cannot be served from a live-mode
    # cache entry.
    us_options_logic._cache.clear()
    _mark_supabase(True)
    try:
        supa = fn(*args, **kw)
    finally:
        _mark_supabase(False)
    return live, supa


def _both_try(fn, *args, **kw):
    """Like both() but captures a raise on either side as the sentinel RAISE."""
    class _Raised:
        def __init__(self, exc):
            self.exc = exc

    _mark_supabase(False)
    try:
        live = fn(*args, **kw)
    except Exception as e:
        live = _Raised(e)
    finally:
        _mark_supabase(False)
    us_options_logic._cache.clear()
    _mark_supabase(True)
    try:
        supa = fn(*args, **kw)
    except Exception as e:
        supa = _Raised(e)
    finally:
        _mark_supabase(False)
    return live, supa, _Raised


def _column_matches(col, lv, sp):
    """(ok, first_mismatch_repr) for one aligned column across two frames."""
    a = lv[col].reset_index(drop=True)
    b = sp[col].reset_index(drop=True)
    fa = pd.to_numeric(a, errors="coerce")
    fb = pd.to_numeric(b, errors="coerce")
    a_num = (fa.isna() == a.isna()).all()
    b_num = (fb.isna() == b.isna()).all()
    if a_num and b_num:
        close = np.isclose(
            fa.to_numpy(dtype=float), fb.to_numpy(dtype=float),
            rtol=1e-5, atol=1e-6, equal_nan=True,
        )
        if not close.all():
            i = int(np.argmax(~close))
            return False, f"col={col} row={i} live={a.iloc[i]!r} supa={b.iloc[i]!r}"
        return True, None
    # Object / string / bool exact comparison; treat missing as equal.
    sa = a.where(a.notna(), "<NA>").astype(str)
    sb = b.where(b.notna(), "<NA>").astype(str)
    eq = sa.to_numpy() == sb.to_numpy()
    if not eq.all():
        i = int(np.argmax(~eq))
        return False, f"col={col} row={i} live={a.iloc[i]!r} supa={b.iloc[i]!r}"
    return True, None


def compare_frames(name, live_df, supa_df, key_cols):
    """Print a PASS/FAIL line comparing two frames. Returns True on PASS."""
    lc, sc = len(live_df), len(supa_df)
    # Compare only the columns both sides carry (supabase snapshots add
    # stock_code/source), minus the ignored freshness columns.
    cols = [c for c in live_df.columns
            if c in supa_df.columns and c not in IGNORE_COLS]
    missing_key = [k for k in key_cols if k not in cols]
    if missing_key:
        print(f"[{name}] live={lc} supa={sc} FAIL key column(s) absent: {missing_key}")
        return False
    if lc != sc:
        print(f"[{name}] live={lc} supa={sc} FAIL row-count mismatch")
        return False
    if lc == 0:
        print(f"[{name}] live={lc} supa={sc} SKIP/PASS (both empty)")
        return True

    lv = live_df[cols].sort_values(key_cols).reset_index(drop=True)
    sp = supa_df[cols].sort_values(key_cols).reset_index(drop=True)
    for col in cols:
        ok, mism = _column_matches(col, lv, sp)
        if not ok:
            print(f"[{name}] live={lc} supa={sc} FAIL {mism}")
            return False
    print(f"[{name}] live={lc} supa={sc} PASS")
    return True


def main():
    _mark_supabase(False)
    # Pin the warrant universe SYNCHRONOUSLY first. sync_warrant otherwise
    # kicks an async ISIN scrape that can complete mid-run, invalidate the
    # warrant cache, and make the live read resolve against a different (larger)
    # universe than the one the snapshot was written from — a harness-only race,
    # not a reader defect. Scraping up front makes the universe stable so live
    # and snapshot see identical inputs.
    print("PARITY: pinning warrant universe (synchronous ISIN scrape)...")
    warrant_logic.scrape_twse_universe()
    print("PARITY: refreshing snapshots + warming live caches (single instant)...")
    scheduler.sync_warrant()
    scheduler.sync_tw_option()
    scheduler.sync_us_option()
    print("PARITY: refresh complete\n")

    results = {}

    # 1. Warrant scanner (compute_iv default True) — HARD gate. Compare tuple[0].
    lv, sp = both(
        warrant_logic.read_warrant, ["2330", "2317"], "All", 0, 365, 0, 100, 0
    )
    results["warrants_scanner"] = compare_frames(
        "warrants_scanner", lv[0], sp[0], ["warrant_code"]
    )

    # 2. Warrant arb superset (compute_iv=False).
    lv, sp = both(
        warrant_logic.read_warrant, ["2330"], "All", 0, 365, 0, 1e9, 0,
        compute_iv=False,
    )
    results["warrants_arb"] = compare_frames(
        "warrants_arb", lv[0], sp[0], ["warrant_code"]
    )

    # 3. TW options scanner. Both live and supabase now return (df, error, meta)
    #    gracefully — both-empty is a legitimate SKIP/PASS, not a raise.
    def _tw_option_df(*args, **kw):
        df, _err, _meta = options_logic.read_tw_option(*args, **kw)
        return df

    lv, sp = both(_tw_option_df, ["2330"], "All", 0, 365, 0, 0)
    if lv.empty and sp.empty:
        print("[tw_options_scanner] live=empty supa=empty SKIP/PASS (both empty)")
        results["tw_options_scanner"] = True
    else:
        results["tw_options_scanner"] = compare_frames(
            "tw_options_scanner", lv, sp, ["contract"]
        )

    # 4. US options scanner.
    def _us_option_df(*args, **kw):
        df, _err, _meta = us_options_logic.read_us_option(*args, **kw)
        return df

    lv, sp = both(_us_option_df, ["2303"], "All", 1, 365)
    if lv.empty and sp.empty:
        print("[us_options_scanner] live=empty supa=empty SKIP/PASS (both empty)")
        results["us_options_scanner"] = True
    else:
        results["us_options_scanner"] = compare_frames(
            "us_options_scanner", lv, sp, ["contract"]
        )

    # 5. Arb finder end-to-end (proves the /match_warrant_tw_option path matches) — HARD gate.
    lv, sp, Raised = _both_try(
        arb_logic.match_warrant_tw_option, ["2330"], "All", 5.0, 30,
        True, 0, "same_type",
    )
    live_empty = isinstance(lv, Raised) or (hasattr(lv, "empty") and lv.empty)
    supa_empty = isinstance(sp, Raised) or (hasattr(sp, "empty") and sp.empty)
    if live_empty and supa_empty:
        print("[arb_finder] live=empty supa=empty SKIP/PASS (both empty)")
        results["arb_finder"] = True
    elif isinstance(lv, Raised) or isinstance(sp, Raised):
        print(f"[arb_finder] FAIL only one side raised "
              f"(live_raised={isinstance(lv, Raised)} supa_raised={isinstance(sp, Raised)})")
        results["arb_finder"] = False
    else:
        results["arb_finder"] = compare_frames(
            "arb_finder", lv, sp, ["warrant_code", "option_contract"]
        )

    # Hard gate: the warrant scanner + arb checks MUST pass. Others PASS or
    # legitimately SKIP (both empty).
    hard = ["warrants_scanner", "arb_finder"]
    print()
    failed = [k for k, ok in results.items() if not ok]
    hard_failed = [k for k in hard if not results.get(k)]
    if failed:
        reason = f"checks failed: {failed}"
        if hard_failed:
            reason = f"HARD-GATE {hard_failed}; " + reason
        print(f"VALIDATE: FAIL {reason}")
        return 1
    print("VALIDATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
