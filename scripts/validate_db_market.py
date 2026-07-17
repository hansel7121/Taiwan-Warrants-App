#!/usr/bin/env python3
"""Standalone gate for Phase 2 Step 1: prove services/db_market.py against the
REAL Supabase tables (env from .env). Idempotent and re-runnable — it cleans up
every row it writes at the end so the real writers later start from empty.

Proves, end to end:
  * >500-row insert chunking AND >1000-row read pagination (1200-row batch),
  * NaN -> None round-trips (not the string 'nan'),
  * insert -> swap -> delete atomicity: a second batch fully replaces the first
    (old batch entirely gone, new batch entirely present, never mixed),
  * codes= filtering,
  * cmoney_key set_key/get_key.

Run:  /opt/anaconda3/envs/warrants/bin/python scripts/validate_db_market.py

If the md_* tables do not yet exist in Supabase you will get a clear
"table ... does not exist" error — the DDL in supabase/schema.sql must be pasted
into the Supabase SQL editor first (supabase-py cannot run DDL).
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import db          # noqa: E402
from services import db_market   # noqa: E402

# md_warrants columns (logic/warrant_logic.py COL_ORDER), excluding batch_id.
_COLS = [
    "warrant_code", "warrant_name", "underlying_code", "type",
    "underlying_price", "ask", "bid", "ask_qty", "bid_qty", "days_to_expiry",
    "strike", "exercise_ratio", "volume", "time_value", "time_value_pct",
    "time_value_am", "iv_ask", "iv_bid", "delta_calc", "leverage_calc",
]


def _make_df(marker, n, underlying):
    """n synthetic warrant rows with realistic md_warrants types. Every 7th row
    has NaN iv_ask so NaN->None can be verified on the round-trip."""
    rows = []
    for i in range(n):
        rows.append({
            "warrant_code": f"{marker}_{i:04d}",
            "warrant_name": f"synthetic {marker} {i}",
            "underlying_code": underlying,
            "type": "Call" if i % 2 == 0 else "Put",
            "underlying_price": 100.0 + i * 0.01,
            "ask": 1.5 + i * 0.001,
            "bid": 1.4 + i * 0.001,
            "ask_qty": float(i % 50),
            "bid_qty": float((i + 1) % 50),
            "days_to_expiry": 30 + (i % 120),
            "strike": 95.0 + (i % 40),
            "exercise_ratio": 0.1,
            "volume": float(i * 3),
            "time_value": 0.5 + i * 0.0005,
            "time_value_pct": 0.5,
            "time_value_am": 0.4,
            "iv_ask": np.nan if i % 7 == 0 else round(0.25 + (i % 30) * 0.001, 4),
            "iv_bid": round(0.24 + (i % 30) * 0.001, 4),
            "delta_calc": round(0.5 + (i % 10) * 0.01, 4),
            "leverage_calc": round(3.0 + (i % 20) * 0.05, 4),
        })
    return pd.DataFrame(rows)[_COLS]


def _cleanup():
    """Remove every row this script could have written so it is re-runnable and
    leaves the real tables empty for the actual writers."""
    # neq on a value no real batch_id will ever equal => delete all rows.
    db._run(lambda c: c.table("md_warrants").delete().neq("batch_id", "00000000-0000-0000-0000-000000000000").execute())
    db._run(lambda c: c.table("md_batches").delete().eq("category", "warrants").execute())
    db._run(lambda c: c.table("cmoney_key").delete().eq("id", 1).execute())


def main():
    # Fresh start so a prior aborted run cannot skew counts.
    _cleanup()

    # ── 1 & 2: 1200-row batch A, round-trip, NaN->None ──────────────────────
    df_a = _make_df("SYNTH_A", 1200, "9991")
    nan_codes = {r["warrant_code"] for r in df_a.to_dict("records")
                 if pd.isna(r["iv_ask"])}
    db_market.write_snapshot("warrants", df_a)
    got, created_at = db_market.read_snapshot("warrants")

    assert len(got) == 1200, f"expected 1200 rows, got {len(got)}"
    assert created_at, "created_at missing on read"

    # A known non-null value round-trips.
    row5 = got[got["warrant_code"] == "SYNTH_A_0005"].iloc[0]
    assert row5["underlying_code"] == "9991", "underlying_code did not round-trip"
    assert abs(float(row5["days_to_expiry"]) - (30 + 5)) < 1e-9, "days_to_expiry mismatch"

    # NaN cells came back as real null (None/NaN), never the string 'nan'.
    nan_row = got[got["warrant_code"] == "SYNTH_A_0000"].iloc[0]
    val = nan_row["iv_ask"]
    assert val is None or (isinstance(val, float) and pd.isna(val)), \
        f"NaN cell did not round-trip as null: {val!r}"
    assert str(val) != "nan" or pd.isna(val), "iv_ask came back as string 'nan'"
    # And a non-NaN iv_ask survived.
    ok_row = got[got["warrant_code"] == "SYNTH_A_0001"].iloc[0]
    assert ok_row["iv_ask"] is not None and not pd.isna(ok_row["iv_ask"]), \
        "non-NaN iv_ask was lost"
    print(f"VALIDATE: batch A 1200 rows OK ({len(nan_codes)} NaN iv_ask cells -> null)")

    # ── 3: batch B of 800 rows fully replaces batch A ───────────────────────
    df_b = _make_df("SYNTH_B", 800, "9992")
    db_market.write_snapshot("warrants", df_b)
    got_b, _ = db_market.read_snapshot("warrants")
    assert len(got_b) == 800, f"expected 800 rows after batch B, got {len(got_b)}"
    stray_a = got_b[got_b["warrant_code"].str.startswith("SYNTH_A")]
    assert len(stray_a) == 0, f"{len(stray_a)} SYNTH_A rows survived the swap"
    all_b = got_b[got_b["warrant_code"].str.startswith("SYNTH_B")]
    assert len(all_b) == 800, "batch B not fully present"
    print("VALIDATE: batch B 800 rows replaced A cleanly (0 A rows, 800 B rows)")

    # ── 4: codes= filtering ─────────────────────────────────────────────────
    got_filtered, _ = db_market.read_snapshot("warrants", codes=["9992"])
    assert len(got_filtered) == 800, f"codes=[9992] expected 800, got {len(got_filtered)}"
    got_none, _ = db_market.read_snapshot("warrants", codes=["0000_nomatch"])
    assert len(got_none) == 0, f"codes filter should exclude all, got {len(got_none)}"
    print("VALIDATE: codes= filtering OK")

    # ── 5: cmoney key store ─────────────────────────────────────────────────
    db_market.set_key("TESTKEY123")
    k = db_market.get_key()
    assert k == "TESTKEY123", f"get_key returned {k!r}"
    print("VALIDATE: cmoney_key set/get OK")

    print("VALIDATE: PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"VALIDATE: FAIL {e}")
        sys.exit(1)
    except Exception as e:
        print(f"VALIDATE: FAIL {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        try:
            _cleanup()
        except Exception as e:
            print(f"VALIDATE: cleanup skipped ({type(e).__name__}: {e})")
