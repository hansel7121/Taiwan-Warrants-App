"""CRUD for the end-of-day arb replay tables (supabase/migrations/027): one
eod_arb_runs row per trading day plus its Direct Match and LP episodes.
Written by scripts/eod_arb_replay.py, read by app.py's /eod_arb_* routes for
the Live Arb tab's EOD Replay subtab.
"""
from datetime import datetime, timezone

from services import db

_BATCH = 500


def mark_running(trade_date, tick_file):
    """Upsert the day's run row as running (so the UI shows a replay in progress)."""
    db.run(lambda c: c.table("eod_arb_runs").upsert({
        "trade_date": trade_date.isoformat(), "status": "running", "tick_file": tick_file,
        "error": None, "finished_at": None,
    }).execute())


def finish_run(trade_date, fields):
    """Set the day's final status and stats."""
    db.run(lambda c: c.table("eod_arb_runs").update(
        {**fields, "finished_at": datetime.now(timezone.utc).isoformat()}).eq("trade_date", trade_date.isoformat()).execute())


def replace_episodes(trade_date, direct_rows, lp_rows):
    """Delete the day's episodes, then insert the new ones in batches — re-running a day is idempotent."""
    d = trade_date.isoformat()
    for table, rows in (("eod_arb_direct_episodes", direct_rows), ("eod_arb_lp_episodes", lp_rows)):
        db.run(lambda c, t=table: c.table(t).delete().eq("trade_date", d).execute())
        for i in range(0, len(rows), _BATCH):
            chunk = rows[i:i + _BATCH]
            db.run(lambda c, t=table, ch=chunk: c.table(t).insert(ch).execute())


def list_runs(limit=120):
    """Most recent run rows, newest first."""
    r = db.run(lambda c: c.table("eod_arb_runs").select("*")
               .order("trade_date", desc=True).limit(limit).execute())
    return r.data or []


def get_run(trade_date):
    r = db.run(lambda c: c.table("eod_arb_runs").select("*")
               .eq("trade_date", trade_date.isoformat()).execute())
    return (r.data or [None])[0]


def list_episodes(table, trade_date):
    """All of one day's episodes from `table`, in start order (paged past the 1,000-row cap)."""
    out, start = [], 0
    while True:
        r = db.run(lambda c: c.table(table).select("*").eq("trade_date", trade_date.isoformat())
                   .order("started_at").range(start, start + 999).execute())
        rows = r.data or []
        out.extend(rows)
        if len(rows) < 1000:
            return out
        start += 1000
