"""Validate Phase-2 Step-3 writers + market gate.

Two parts, no live clock for the gate:

1. is_market_open — assert against FIXED injected tz-aware datetimes.
2. Writers fill tables — call the CORE writers directly (bypassing cron+gate),
   then read each snapshot back and assert the row counts / columns are sane.

Run with:
    TZ=Asia/Taipei OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
        /opt/anaconda3/envs/warrants/bin/python scripts/validate_writers.py

This WRITES real snapshots to Supabase — intended; the real writers overwrite
them on the next scheduled run.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import scheduler
from services import db_market

TW = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")

# 2026-07-13 is a Monday, so:
#   Wed = 07-15, Thu = 07-16, Fri = 07-17, Sat = 07-18.
_fails = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got} want={want}")
    if not ok:
        _fails.append(label)


def gate_assertions():
    print("PART 1 — is_market_open (fixed injected datetimes)")

    io = scheduler.is_market_open
    # tw_equity: Asia/Taipei, weekday 0..4, 09:00–13:30.
    check("tw_equity Wed 10:00 open", io("tw_equity", datetime(2026, 7, 15, 10, 0, tzinfo=TW)), True)
    check("tw_equity Wed 14:00 closed", io("tw_equity", datetime(2026, 7, 15, 14, 0, tzinfo=TW)), False)
    check("tw_equity Sat 10:00 closed", io("tw_equity", datetime(2026, 7, 18, 10, 0, tzinfo=TW)), False)

    # tw_option: day [08:45–13:45] or night [Mon–Fri >=15:00 or Tue–Sat <05:00].
    check("tw_option Wed 10:00 open (day)", io("tw_option", datetime(2026, 7, 15, 10, 0, tzinfo=TW)), True)
    check("tw_option Wed 22:00 open (night)", io("tw_option", datetime(2026, 7, 15, 22, 0, tzinfo=TW)), True)
    check("tw_option Thu 03:00 open (night spillover)", io("tw_option", datetime(2026, 7, 16, 3, 0, tzinfo=TW)), True)
    check("tw_option Wed 06:00 closed", io("tw_option", datetime(2026, 7, 15, 6, 0, tzinfo=TW)), False)
    check("tw_option Sat 06:00 closed", io("tw_option", datetime(2026, 7, 18, 6, 0, tzinfo=TW)), False)
    check("tw_option Sat 03:00 open (Fri-night spillover)", io("tw_option", datetime(2026, 7, 18, 3, 0, tzinfo=TW)), True)

    # us_option: America/New_York, weekday 0..4, 09:30–16:00 ET.
    check("us_option Wed 10:00 ET open", io("us_option", datetime(2026, 7, 15, 10, 0, tzinfo=NY)), True)
    check("us_option Wed 09:00 ET closed", io("us_option", datetime(2026, 7, 15, 9, 0, tzinfo=NY)), False)
    check("us_option Wed 16:30 ET closed", io("us_option", datetime(2026, 7, 15, 16, 30, tzinfo=NY)), False)
    check("us_option Sat 10:00 ET closed", io("us_option", datetime(2026, 7, 18, 10, 0, tzinfo=NY)), False)


# Expected data columns per category (schema minus batch_id).
_COLS = {
    "warrants": set(scheduler.warrant_logic.COL_ORDER),
    "tw_options": set(scheduler.TW_OPTION_COLS),
    "us_options": set(scheduler.US_OPTION_COLS),
    "warrant_universe": {"code", "name", "start", "market"},
}


def _read_twice(category):
    """read_snapshot twice; return (df, count). Assert count is stable at rest."""
    df1, _ = db_market.read_snapshot(category)
    df2, _ = db_market.read_snapshot(category)
    n1, n2 = len(df1), len(df2)
    if n1 != n2:
        _fails.append(f"{category} unstable read {n1} != {n2}")
        print(f"  [FAIL] {category} stable-read: {n1} != {n2}")
    else:
        print(f"  [PASS] {category} stable-read: {n1} rows both times")
    return df1, n1


def _check_cols(category, df):
    if df.empty:
        print(f"  [note] {category}: 0 rows, skipping column check")
        return
    got = set(df.columns)
    want = _COLS[category]
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {category} columns match schema")
    if not ok:
        _fails.append(f"{category} columns {got ^ want}")


def writer_assertions():
    print("\nPART 2 — writers fill tables (core writers, direct)")

    print(" sync_cmkey()")
    scheduler.sync_cmkey()
    key = db_market.get_key()
    if key:
        print(f"  [PASS] cmoney_key non-empty (len={len(key)})")
    else:
        _fails.append("cmoney_key empty")
        print("  [FAIL] cmoney_key empty")

    print(" sync_universe()")
    scheduler.sync_universe()
    df, n = _read_twice("warrant_universe")
    _check_cols("warrant_universe", df)
    if n > 10000:
        print(f"  [PASS] warrant_universe {n} rows (> 10000)")
    else:
        _fails.append(f"warrant_universe only {n} rows")
        print(f"  [FAIL] warrant_universe only {n} rows (want > 10000)")

    print(" sync_warrant()")
    scheduler.sync_warrant()
    df, n = _read_twice("warrants")
    _check_cols("warrants", df)
    if n > 1000:
        print(f"  [PASS] warrants {n} rows (> 1000)")
    else:
        _fails.append(f"warrants only {n} rows")
        print(f"  [FAIL] warrants only {n} rows (want > 1000)")

    print(" sync_tw_option()")
    scheduler.sync_tw_option()
    df, n = _read_twice("tw_options")
    _check_cols("tw_options", df)
    if n > 0:
        print(f"  [PASS] tw_options {n} rows (> 0, columns verified)")
    else:
        print("  [note] tw_options 0 rows — acceptable off-hours, SKIP >0 assertion")

    print(" sync_us_option()")
    scheduler.sync_us_option()
    df, n = _read_twice("us_options")
    _check_cols("us_options", df)
    if n > 0:
        print(f"  [PASS] us_options {n} rows (> 0, columns verified)")
    else:
        print("  [note] us_options 0 rows — acceptable off-hours, SKIP >0 assertion")


def main():
    gate_assertions()
    writer_assertions()
    print()
    if _fails:
        print(f"VALIDATE: FAIL {_fails}")
        sys.exit(1)
    print("VALIDATE: PASS")


if __name__ == "__main__":
    main()
