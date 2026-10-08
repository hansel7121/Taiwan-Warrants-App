"""One-off seed for the Forced Short Squeeze paper trader from the QFS-Pitch-Code "Taiwan Pitch/data" folder.

Writes the historical quintile pool (every suspension.csv deadline, one per stock per 10 trading days, with margin
shorts on D-17 — the notebook's `pool`) to fss_pool, and the most recent trading days of prices / short balances
to FSS_DATA_DIR/panel so the first run does not have to backfill them from TWSE.

Usage:
    python scripts/fss_seed.py --qfs-data "/path/to/Taiwan Pitch/data" [--panel-days 390] [--no-pool] [--no-panel]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logic import fss_logic as F  # noqa: E402
from services import fss  # noqa: E402


def load(data):
    is_stock = lambda s: s.str.fullmatch(r"[1-9]\d{3}")
    px = pd.read_csv(os.path.join(data, "prices.csv"), dtype={"stock_id": str}, parse_dates=["date"],
                     usecols=["date", "stock_id", "name", "close", "volume", "value"])
    px = px[is_stock(px.stock_id)].drop_duplicates(["date", "stock_id"])
    mg = pd.read_csv(os.path.join(data, "margin.csv"), dtype={"stock_id": str}, parse_dates=["date"],
                     usecols=["date", "stock_id", "short_bal"])
    mg = mg[is_stock(mg.stock_id)].drop_duplicates(["date", "stock_id"])
    ix = pd.read_csv(os.path.join(data, "index.csv"), parse_dates=["date"])[["date", "taiex_tr"]]
    ex = pd.read_csv(os.path.join(data, "exdiv.csv"), dtype={"stock_id": str}, parse_dates=["date"])
    ex = ex.dropna(subset=["close_before", "ref_price"]).drop_duplicates(["date", "stock_id"])
    ex["adj"] = ex.ref_price / ex.close_before
    return px, mg, ix, ex[["date", "stock_id", "adj"]]


def pool_rows(data, panel):
    """The notebook's Q5 pool, as fss_pool rows."""
    sus = pd.read_csv(os.path.join(data, "suspension.csv"), dtype={"stock_id": str}, parse_dates=["date"]).drop_duplicates()
    cal = pd.DatetimeIndex(panel.dates)
    sus["D"] = cal.searchsorted(sus.date.values)
    p = sus[sus.stock_id.isin(panel.col) & (sus.D >= F.SIG_LAG) & (sus.D < panel.T)].sort_values(["stock_id", "D"])
    p = p[p.groupby("stock_id").D.diff().fillna(99) >= F.EVENT_GAP]
    j, s = p.stock_id.map(panel.col).values, p.D.values - F.SIG_LAG
    sb = panel.SB[s, j]
    dtc = sb / panel.ADV[s, j]
    ok = (sb > 0) & np.isfinite(dtc)
    p = p[ok]
    return [{"id": f"{sid}:{cal[d].date().isoformat()}", "stock_id": sid, "d_date": cal[d].date().isoformat(),
             "reason": reason, "short_bal": float(b), "dtc": float(x)}
            for sid, d, reason, b, x in zip(p.stock_id, p.D, p.reason, sb[ok], dtc[ok])]


def write_panel(px, mg, ix, ex, n_days):
    days = sorted(ix.date.drop_duplicates())[-n_days:]
    keep = set(days)
    px, mg, ex = px[px.date.isin(keep)], mg[mg.date.isin(keep)], ex[ex.date.isin(keep)]
    tr = ix.drop_duplicates("date").set_index("date").taiex_tr
    pg, mgg, exg = px.groupby("date"), mg.groupby("date"), ex.groupby("date")
    for d in days:
        p = pg.get_group(d) if d in pg.groups else px.iloc[:0]
        m = mgg.get_group(d) if d in mgg.groups else mg.iloc[:0]
        e = exg.get_group(d) if d in exg.groups else ex.iloc[:0]
        fss.write_day(d.date(), {r.stock_id: [r.name, r.close, r.volume, r.value] for r in p.itertuples()},
                      dict(zip(m.stock_id, m.short_bal)), float(tr[d]), dict(zip(e.stock_id, e.adj)))
    print(f"wrote {len(days)} day files {days[0].date()} -> {days[-1].date()} to {fss.PANEL_DIR}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qfs-data", required=True)
    ap.add_argument("--panel-days", type=int, default=fss.KEEP_FILES)
    ap.add_argument("--no-pool", action="store_true")
    ap.add_argument("--no-panel", action="store_true")
    a = ap.parse_args()
    px, mg, ix, ex = load(a.qfs_data)
    if not a.no_panel:
        write_panel(px, mg, ix, ex, a.panel_days)
    if not a.no_pool:
        from services import db_fss
        panel = F.Panel(px, mg, ix, ex)
        rows = pool_rows(a.qfs_data, panel)
        db_fss.replace_pool(rows)
        print(f"seeded fss_pool with {len(rows):,} deadlines, {rows[0]['d_date']} -> {max(r['d_date'] for r in rows)}")


if __name__ == "__main__":
    main()
