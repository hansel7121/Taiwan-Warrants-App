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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd

from services import db

# category -> (table name, code column for the read/delete filter, ORDER-BY
# columns for deterministic pagination). The md_* tables have NO single-column
# primary key (see supabase/schema.sql), so ranged reads must carry an explicit,
# stable ORDER BY or a range window may return a different slice than the one
# before it. warrant_code and universe `code` are unique per row; option rows are
# keyed on (stock_code, contract, type). See read_snapshot for why a non-unique
# option key is still safe for pagination here.
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
    """Return (DataFrame, created_at_iso_or_None) for the current batch.

    Reads are paginated and filtered to the current batch_id; if `codes` is given
    they are also filtered on the category's code column. No other filtering is
    applied — callers filter in pandas.

    Pagination is count-driven, NOT early-break-on-short-page. PostgREST enforces
    a server-side max-rows cap (db-max-rows, default 1000): a `.range()` window
    wider than the cap silently returns only the cap, so we can never trust the
    REQUESTED page size (_READ_PAGE) as the real one. The first request carries
    count="exact" — that returns page 1 AND the true total row count for the batch
    — and we measure page 1's length to learn the effective server page size. The
    remaining ranges are then fetched CONCURRENTLY (serial paging was the "Loading
    market data from database…" latency on every fetch). Every ranged query
    carries an explicit ORDER BY (the md_* tables have no single-column PK) so
    range windows tile the batch without overlap or gaps.

    Order-key uniqueness: warrant_code / universe `code` are per-row unique. The
    option key (stock_code, contract, type) can tie (the tw_options MIS+EOD union
    can hold byte-identical rows), but a non-unique ORDER BY is still safe for
    pagination HERE because tied rows are identical: even if the server returned a
    tie group in a different relative order for two adjacent page queries, each
    position in the window is still filled by one member of the group, so the
    returned multiset — hence the row count and the frame content — is invariant.
    A drop/dup would require tied rows with DIFFERING content, which this key does
    not produce. Return contract (DataFrame minus batch_id) and `codes` filtering
    are unchanged.
    """
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
        """One ordered ranged read of the current batch: positions
        [offset, offset+page-1], filtered to `codes` if given. Pass
        count="exact" to also carry the batch's total row count on the response
        (.count). Each call builds its own independent query object, so this is
        safe to submit concurrently to the pool below; db._run is the shared
        transport-retry wrapper."""
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
        # 2. Fetch every remaining range concurrently. Page size is the measured
        # server cap, so windows never exceed it (no truncation). Collect pages
        # (including page 1) keyed by offset in a dict as they complete
        # out-of-order, then drain them in offset order into the final `rows`
        # list, popping each as it is consumed. This keeps only ONE full copy of
        # the row references at peak: the `pages` dict shrinks (pop) exactly as
        # `rows` grows, so — unlike the original, which grew `rows` while still
        # holding the whole `pages` dict — we never retain two full copies at
        # once. (The row dicts themselves are shared references throughout; what
        # is avoided is a second full set of page-list containers.)
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
