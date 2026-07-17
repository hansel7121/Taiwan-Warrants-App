"""Supabase Postgres access (service-role) for SERVER-ONLY market-data snapshots.

Mirrors services/db.py exactly: no top-level `supabase` import, the client is
reused via `db.client()`, and every query goes through `db._run(build)` (the
transport-retry wrapper). Following db.py, the core functions carry NO internal
try/except — callers decide how to handle failures.

Snapshot model (see supabase/schema.sql): each category is a set of rows tagged
by a batch_id; md_batches holds the current-generation batch_id per category.
write_snapshot does INSERT (new batch) -> SWAP POINTER (md_batches upsert) ->
DELETE-OLD (prior batches), never delete-first. supabase-py has no transactions,
so the pointer flip IS the atomicity mechanism: a concurrent reader always sees
one complete batch (old or new), never an empty or half-written one.

This module MAY import pandas (it is a market-data module).
"""
import os
import uuid
from datetime import datetime, timezone

import pandas as pd

from services import db

# category -> (table name, code column used for the read/delete filter).
_CATEGORY = {
    "warrants":          ("md_warrants",          "underlying_code"),
    "tw_options":        ("md_tw_options",        "stock_code"),
    "us_options":        ("md_us_options",        "stock_code"),
    "warrant_universe":  ("md_warrant_universe",  "code"),
}

_INSERT_CHUNK = 500   # rows per insert request
_READ_PAGE = 1000     # rows per read page (.range window)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _records(df, batch_id):
    """DataFrame -> list of JSON-safe records with batch_id attached.

    NaN/NaT must become None: supabase-py serializes to JSON and a bare NaN is
    not valid JSON (and 'nan' as a string would corrupt the column). astype
    (object).where(notnull, None) replaces every missing cell with None.
    """
    clean = df.astype(object).where(pd.notnull(df), None)
    records = clean.to_dict(orient="records")
    for r in records:
        r["batch_id"] = batch_id
    return records


def write_snapshot(category, df):
    """Insert df as a brand-new batch for `category`, atomically swap it in, then
    drop the old batch. Returns the new batch_id.

    Order is INSERT -> SWAP POINTER -> DELETE-OLD. The pointer flip is the
    commit point; never delete before it.
    """
    table, _code_col = _CATEGORY[category]
    batch_id = str(uuid.uuid4())

    # 1. INSERT the new batch, chunked so no single request is oversized.
    records = _records(df, batch_id)
    for i in range(0, len(records), _INSERT_CHUNK):
        chunk = records[i:i + _INSERT_CHUNK]
        db._run(lambda c, chunk=chunk: c.table(table).insert(chunk).execute())

    # 2. SWAP POINTER — this upsert is the atomic commit of the new batch.
    db._run(
        lambda c: c.table("md_batches")
        .upsert(
            {"category": category, "batch_id": batch_id, "created_at": _now()},
            on_conflict="category",
        )
        .execute()
    )

    # 3. DELETE-OLD — everything in this table that is not the new batch.
    db._run(
        lambda c: c.table(table).delete().neq("batch_id", batch_id).execute()
    )
    return batch_id


def read_snapshot(category, codes=None):
    """Return (DataFrame, created_at_iso_or_None) for the current batch.

    Reads are paginated in 1000-row pages and filtered to the current batch_id;
    if `codes` is given they are also filtered on the category's code column.
    No other filtering is applied — callers filter in pandas.
    """
    table, code_col = _CATEGORY[category]

    ptr = db._run(
        lambda c: c.table("md_batches")
        .select("batch_id, created_at")
        .eq("category", category)
        .execute()
    )
    if not ptr.data:
        return pd.DataFrame(), None
    batch_id = ptr.data[0]["batch_id"]
    created_at = ptr.data[0].get("created_at")

    rows = []
    offset = 0
    while True:
        def build(c, offset=offset):
            q = c.table(table).select("*").eq("batch_id", batch_id)
            if codes is not None:
                q = q.in_(code_col, list(codes))
            return q.range(offset, offset + _READ_PAGE - 1).execute()

        page = db._run(build)
        batch = page.data or []
        rows.extend(batch)
        if len(batch) < _READ_PAGE:
            break
        offset += _READ_PAGE

    df = pd.DataFrame(rows)
    if not df.empty and "batch_id" in df.columns:
        df = df.drop(columns=["batch_id"])
    return df, created_at


def snapshot_enabled():
    """True when readers should serve the Supabase snapshot first.

    Gated on MARKET_SOURCE=supabase; read at call time so the env can be
    toggled per call (the parity gate relies on this).
    """
    return os.environ.get("MARKET_SOURCE") == "supabase"


def snapshot_as_of(category):
    """Return the current batch's md_batches.created_at ISO string, or None."""
    ptr = db._run(
        lambda c: c.table("md_batches")
        .select("created_at")
        .eq("category", category)
        .execute()
    )
    if not ptr.data:
        return None
    return ptr.data[0].get("created_at")


def get_key():
    """Return the stored CMoney key string, or None if unset."""
    r = db._run(
        lambda c: c.table("cmoney_key").select("key").eq("id", 1).execute()
    )
    if r.data:
        return r.data[0].get("key")
    return None


def set_key(key):
    """Upsert the single-row CMoney key store (id=1)."""
    db._run(
        lambda c: c.table("cmoney_key")
        .upsert({"id": 1, "key": key, "updated_at": _now()})
        .execute()
    )
    return True
