"""CRUD for the Forced Short Squeeze paper-trading tables (supabase/migrations/028): the seeded quintile pool,
scraped deadline events, paper trades and run log. Written by services/fss.py, read by app.py's /fss_* routes.
"""
from datetime import datetime, timezone

from services import db

_BATCH = 500


def _all(table, build=lambda q: q):
    """Every row of `table` (paged past the 1,000-row cap)."""
    out, start = [], 0
    while True:
        r = db.run(lambda c: build(c.table(table).select("*")).order("id").range(start, start + 999).execute())
        rows = r.data or []
        out.extend(rows)
        if len(rows) < 1000:
            return out
        start += 1000


def _upsert(table, rows):
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        db.run(lambda c, ch=chunk: c.table(table).upsert(ch).execute())


def list_pool():
    return _all("fss_pool")


def replace_pool(rows):
    """Seed: replace the whole pool."""
    db.run(lambda c: c.table("fss_pool").delete().neq("id", "").execute())
    _upsert("fss_pool", rows)


def list_events(since=None):
    """Every event, or those with a deadline on/after `since`."""
    return _all("fss_events", (lambda q: q.gte("d_date", since.isoformat())) if since else (lambda q: q))


def upsert_events(rows):
    _upsert("fss_events", rows)


def list_trades():
    return _all("fss_trades")


def upsert_trades(rows):
    _upsert("fss_trades", rows)


def start_run(kind):
    run_at = datetime.now(timezone.utc).isoformat()
    db.run(lambda c: c.table("fss_runs").insert({"run_at": run_at, "kind": kind, "status": "running"}).execute())
    return run_at


def finish_run(run_at, fields):
    db.run(lambda c: c.table("fss_runs").update(fields).eq("run_at", run_at).execute())


def list_runs(limit=20):
    r = db.run(lambda c: c.table("fss_runs").select("*").order("run_at", desc=True).limit(limit).execute())
    return r.data or []
