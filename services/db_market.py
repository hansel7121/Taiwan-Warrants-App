"""Supabase Postgres access (service-role) for SERVER-ONLY market-data snapshots.

Batch-pointer model (see supabase/schema.sql): each category is a set of rows
tagged by a batch_id, with md_batches holding the current batch_id per category.
write_snapshot does INSERT (new batch) -> SWAP POINTER -> DELETE-OLD, so a
concurrent reader always sees one complete batch — the pointer flip is the
atomicity mechanism since supabase-py has no transactions.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd

from services import db

# category -> (table name, code column for read/delete filter, ORDER-BY columns
# for deterministic pagination — the md_* tables have no single-column PK, so a
# range read needs an explicit stable order).
_CATEGORY = {
    "warrants":          ("md_warrants",          "underlying_code", ("warrant_code",)),
    "tw_options":        ("md_tw_options",        "stock_code",      ("stock_code", "contract", "type")),
    "us_options":        ("md_us_options",        "stock_code",      ("stock_code", "contract", "type")),
    "warrant_universe":  ("md_warrant_universe",  "code",            ("code",)),
}

_INSERT_CHUNK = 500   # rows per insert request
_READ_PAGE = 1000     # requested rows per read page (.range window)
_READ_WORKERS = 6     # concurrent range fetches for a multi-page snapshot


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
    table, _code_col, _order_cols = _CATEGORY[category]
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
    """Paginated, concurrently-fetched (DataFrame, created_at_iso_or_None) for the current batch, optionally filtered to `codes`."""
    # PostgREST caps .range() width server-side (db-max-rows, default 1000), so
    # the requested _READ_PAGE can't be trusted as the real page size: the first
    # request carries count="exact" to learn both the true total and the
    # effective page size from page 1's length, then remaining ranges are
    # fetched concurrently. Every ranged query carries an explicit ORDER BY
    # (md_* tables have no single-column PK) so windows tile without gaps; a
    # non-unique order key (the option key can tie) is still safe here because
    # tied rows are identical, so the returned multiset is invariant regardless
    # of tie order.
    table, code_col, order_cols = _CATEGORY[category]

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

    def fetch(offset, page, count=None):
        """Ordered ranged read [offset, offset+page-1] of the current batch, filtered to `codes` if given; count="exact" also returns the batch's total row count."""
        def build(c):
            q = c.table(table).select("*", count=count) if count else \
                c.table(table).select("*")
            q = q.eq("batch_id", batch_id)
            if codes is not None:
                q = q.in_(code_col, list(codes))
            for col in order_cols:
                q = q.order(col)
            return q.range(offset, offset + page - 1).execute()

        return db._run(build)

    # 1. First page WITH an exact count: gives page 1's rows and the true total.
    first = fetch(0, _READ_PAGE, count="exact")
    first_rows = list(first.data or [])
    page_size = len(first_rows)   # EFFECTIVE server page size (capped, not _READ_PAGE)
    total = first.count           # exact row count for this batch + codes filter

    if page_size and total and total > page_size:
        # Fetch remaining ranges concurrently, collect out-of-order by offset,
        # then drain into `rows` in order — popping each page as consumed keeps
        # only one full copy of the row references at peak.
        offsets = list(range(page_size, total, page_size))
        pages = {0: first_rows}
        with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(offsets))) as ex:
            futures = {ex.submit(fetch, off, page_size): off for off in offsets}
            for fut in as_completed(futures):
                off = futures[fut]
                pages[off] = fut.result().data or []
        rows = []
        for off in [0] + offsets:
            rows.extend(pages.pop(off))
    elif page_size and total is None:
        # Fallback: count unavailable. Page sequentially and break on the MEASURED
        # page size — never on _READ_PAGE — so a server cap below _READ_PAGE can
        # never be mistaken for end-of-data and truncate the snapshot.
        rows = first_rows
        offset = page_size
        while True:
            page = fetch(offset, page_size)
            batch = page.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
    else:
        # Single page covers the whole batch (total <= page_size, or empty).
        rows = first_rows

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
