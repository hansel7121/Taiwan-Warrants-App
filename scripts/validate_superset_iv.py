"""Proves `keep_noniv=True` produces a row set equal to the compute_iv=False
superset, while its notnull-IV subset reproduces the compute_iv=True scanner
set exactly. `build_warrant_df` is checked hard (pure, market-hours-independent);
TW/US option legs are best-effort and skip rather than fail off-hours.

Run:
  TZ=Asia/Taipei OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
    /opt/anaconda3/envs/warrants/bin/python scripts/validate_superset_iv.py
"""
import os
import sys
import traceback

import numpy as np
import pandas as pd

# Make the repo root importable when run as `python scripts/validate_superset_iv.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logic import warrant_logic
from logic import options_logic
from logic import us_options_logic

_failures = []


def _fail(reason):
    _failures.append(reason)
    print(f"  FAIL: {reason}")


def _ok(msg):
    print(f"  ok: {msg}")


def _num(df, cols):
    """Return a copy with the given columns coerced to float (None -> NaN)."""
    out = df.copy()
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _identity_set(df, identity):
    return set(map(tuple, df[identity].astype(object).values.tolist()))


def three_way(label, A, B, C, identity, metrics):
    """A=scanner (compute_iv=True), B=superset (compute_iv=False),
    C=new (compute_iv=True, keep_noniv=True). Assert the relationships."""
    print(f"[{label}] A(scanner)={len(A)}  B(superset)={len(B)}  C(new)={len(C)}")

    # 1) C's row set == B's superset (count + identity set).
    if len(C) == len(B):
        _ok(f"len(C)==len(B) ({len(C)})")
    else:
        _fail(f"{label}: len(C)={len(C)} != len(B)={len(B)}")

    setB, setC = _identity_set(B, identity), _identity_set(C, identity)
    if setB == setC:
        _ok(f"C identity set == B identity set ({len(setC)} rows)")
    else:
        only_c, only_b = setC - setB, setB - setC
        _fail(f"{label}: C/B identity sets differ "
              f"(C-only={len(only_c)}, B-only={len(only_b)})")

    # 2) len(A) <= len(C)
    if len(A) <= len(C):
        _ok(f"len(A)<=len(C) ({len(A)}<={len(C)})")
    else:
        _fail(f"{label}: len(A)={len(A)} > len(C)={len(C)}")

    # 3) Scanner view of the stored superset == today's scanner output.
    #    Subset of C where iv_ask notnull, on identity+metrics, == A exactly.
    An = _num(A, metrics)
    Cn = _num(C, metrics)
    Cn = Cn[Cn["iv_ask"].notna()].copy()

    if len(Cn) != len(A):
        _fail(f"{label}: C[iv_ask notnull]={len(Cn)} != A={len(A)}")
        return

    setA = _identity_set(An, identity)
    setCn = _identity_set(Cn, identity)
    if setA != setCn:
        _fail(f"{label}: C-notnull-iv identity set != A identity set "
              f"(A-only={len(setA - setCn)}, C-only={len(setCn - setA)})")
        return
    _ok(f"C[iv_ask notnull] rows == A rows ({len(A)})")

    # Row-for-row value equality on a stable sort by identity columns.
    As = An.sort_values(identity).reset_index(drop=True)
    Cs = Cn.sort_values(identity).reset_index(drop=True)

    mismatched = []
    for c in metrics:
        a = As[c].to_numpy(dtype=float)
        cc = Cs[c].to_numpy(dtype=float)
        if not np.allclose(a, cc, rtol=1e-9, atol=1e-9, equal_nan=True):
            n_bad = int((~np.isclose(a, cc, rtol=1e-9, atol=1e-9, equal_nan=True)).sum())
            mismatched.append(f"{c}({n_bad})")
    if mismatched:
        _fail(f"{label}: metric mismatch on {', '.join(mismatched)}")
    else:
        _ok(f"C[iv_ask notnull] values == A values on {metrics}")


# ── HARD GATE: warrants (pure function of cmoney_results) ────────────────────
print("=== WARRANTS (hard gate) ===")
try:
    stocks = ["2330", "2317", "2454"]
    # get_warrant_results returns (merged {warrant_code: raw_result}, as_of, cached),
    # which is exactly build_warrant_df's input — one raw set, three build modes.
    raw, _as_of, _cached = warrant_logic.get_warrant_results(stocks)
    print(f"raw cmoney results: {len(raw)} warrant codes for {stocks}")
    if not raw:
        _fail("warrants: get_warrant_results returned nothing — cannot run hard gate")
    else:
        A = warrant_logic.build_warrant_df(raw, compute_iv=True)
        B = warrant_logic.build_warrant_df(raw, compute_iv=False)
        C = warrant_logic.build_warrant_df(raw, compute_iv=True, keep_noniv=True)
        identity = ["warrant_code", "underlying_code", "type", "strike", "days_to_expiry"]
        metrics = ["iv_ask", "iv_bid", "delta_calc", "leverage_calc"]
        three_way("warrants", A, B, C, identity, metrics)
except Exception as e:
    traceback.print_exc()
    _fail(f"warrants: exception {e}")


# ── BEST-EFFORT: TW options (SKIP if empty off-hours) ────────────────────────
print("\n=== TW OPTIONS (best-effort) ===")
try:
    def _opt(civ, keep):
        df, err, _meta = options_logic.read_tw_option(["2330"], compute_iv=civ, keep_noniv=keep)
        if df.empty:
            raise RuntimeError(err or "no data")
        return df

    try:
        A = _opt(True, False)
        B = _opt(False, False)
        C = _opt(True, True)
    except RuntimeError as e:
        print(f"  SKIP: TW options returned no data ({e})")
        A = B = C = None

    if A is not None:
        if A.empty or B.empty or C.empty:
            print("  SKIP: TW options empty off-hours")
        else:
            identity = ["contract", "type", "strike", "days_to_expiry"]
            metrics = ["iv_ask", "iv_bid", "delta_calc", "leverage_calc"]
            three_way("tw_options", A, B, C, identity, metrics)
except Exception as e:
    traceback.print_exc()
    _fail(f"tw_options: exception {e}")


# ── BEST-EFFORT: US options (SKIP if empty off-hours) ────────────────────────
print("\n=== US OPTIONS (best-effort) ===")
try:
    def _usopt(civ, keep):
        df, err, _meta = us_options_logic.read_us_option(
            ["2303"], "All", min_days=1, max_days=730, compute_iv=civ, keep_noniv=keep
        )
        if df.empty:
            raise RuntimeError(err or "no data")
        return df

    try:
        A = _usopt(True, False)
        B = _usopt(False, False)
        C = _usopt(True, True)
    except RuntimeError as e:
        print(f"  SKIP: US options returned no data ({e})")
        A = B = C = None

    if A is not None:
        if A.empty or B.empty or C.empty:
            print("  SKIP: US options empty off-hours")
        else:
            identity = ["contract", "type", "strike", "days_to_expiry"]
            metrics = ["iv_ask", "iv_bid", "delta_calc"]  # US leg has no leverage_calc
            three_way("us_options", A, B, C, identity, metrics)
except Exception as e:
    traceback.print_exc()
    _fail(f"us_options: exception {e}")


print()
if _failures:
    print(f"VALIDATE: FAIL {len(_failures)} check(s): " + " | ".join(_failures))
    sys.exit(1)
else:
    print("VALIDATE: PASS")
    sys.exit(0)
