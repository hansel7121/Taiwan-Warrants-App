"""Supabase Postgres access (service-role) for per-user portfolio / custom stocks.

The client is created lazily so the module imports fine without any env vars
(local no-auth dev, sanity checks). A tiny .env loader runs at import so
SUPABASE_* vars in a local .env are visible to this module and auth.py.
"""
import json
import os
from datetime import datetime, timezone


def _load_dotenv():
    """Minimal .env loader (python-dotenv is not a dependency). Silent on any error."""
    # .env lives at the repo root; this module is services/db.py, so go up one
    # directory from here. (Before the logic/services restructure db.py sat at
    # the root and a single dirname sufficed — the extra level is required now,
    # else local mode's LOCAL_USER_ID never loads and every route 503s.)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"DB: .env load skipped: {e}", flush=True)


_load_dotenv()

_client = None


def client():
    """Lazily create the service-role supabase-py client. Raises if unconfigured."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("Supabase not configured")
        from supabase import create_client
        _client = create_client(url, key)
    return _client


def _reset_client():
    """Drop the cached client so the next call() builds a fresh connection pool."""
    global _client
    _client = None


def _run(build):
    """Execute build(client) and retry once on a transport-level failure.

    The client is a long-lived singleton, so its pooled HTTP/2 keep-alive
    connection can be closed by Supabase after idling; the next reuse then raises
    httpx.RemoteProtocolError ("Server disconnected") or a similar transport
    error. That is transient: discard the stale client and rebuild the query
    against a fresh connection. A second failure is real and propagates.
    """
    import httpx
    try:
        return build(client())
    except httpx.TransportError as e:
        print(f"DB: transport error ({type(e).__name__}: {e}); resetting client and retrying once", flush=True)
        _reset_client()
        return build(client())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _payload_key(payload):
    """Stable, order-independent serialization for comparing two payload dicts."""
    return json.dumps(payload, sort_keys=True, default=str)


def get_portfolio(user_id):
    """Payloads of this user's LIVE rows only (deleted_at is null)."""
    r = _run(
        lambda c: c.table("portfolio")
        .select("payload")
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .execute()
    )
    return [row["payload"] for row in (r.data or [])]


def save_portfolio(user_id, entries):
    """Sync-safe diff + tombstone (never hard-delete, so a concurrent writer's
    rows survive). Upsert only entries that are new, changed, or resurrected;
    tombstone live rows no longer present in the posted array. A live row whose
    payload is unchanged is left untouched so updated_at (the sync cursor) is not
    bumped, otherwise reconcile pulls would loop forever.
    """
    now = _now()

    existing = _run(
        lambda c: c.table("portfolio")
        .select("id, payload, deleted_at")
        .eq("user_id", user_id)
        .execute()
    )
    by_id = {str(row["id"]): row for row in (existing.data or [])}

    rows = []
    posted_ids = set()
    for e in entries or []:
        eid = str(e.get("id"))
        posted_ids.add(eid)
        cur = by_id.get(eid)
        if (
            cur is None
            or cur.get("deleted_at") is not None
            or _payload_key(cur.get("payload")) != _payload_key(e)
        ):
            rows.append(
                {
                    "user_id": user_id,
                    "id": eid,
                    "payload": e,
                    "deleted_at": None,
                    "updated_at": now,
                }
            )
        # else: identical live row -> do NOT upsert (must not bump updated_at).

    # Tombstone live rows that are no longer posted. Already-tombstoned rows are
    # left as-is so their cursor is not needlessly bumped.
    for eid, row in by_id.items():
        if eid not in posted_ids and row.get("deleted_at") is None:
            rows.append(
                {
                    "user_id": user_id,
                    "id": eid,
                    "payload": row.get("payload"),
                    "deleted_at": now,
                    "updated_at": now,
                }
            )

    if rows:
        _run(lambda c: c.table("portfolio").upsert(rows).execute())
    return True


def changed_rows_since(user_id, since_iso=None):
    """Raw rows (id, payload, deleted_at, updated_at) for this user, newest first,
    INCLUDING tombstones. If since_iso is given, only rows with updated_at greater
    than it; otherwise all rows. Used by the sync/reconcile layer as its cursor."""
    def build(c):
        q = (
            c.table("portfolio")
            .select("id, payload, deleted_at, updated_at")
            .eq("user_id", user_id)
        )
        if since_iso is not None:
            q = q.gt("updated_at", since_iso)
        return q.order("updated_at", desc=True).execute()

    r = _run(build)
    return r.data or []


def get_custom_stocks(user_id):
    r = _run(
        lambda c: c.table("custom_stocks").select("stocks").eq("user_id", user_id).execute()
    )
    if r.data:
        return r.data[0].get("stocks") or []
    return []


def save_custom_stocks(user_id, stocks):
    _run(
        lambda c: c.table("custom_stocks")
        .upsert({"user_id": user_id, "stocks": stocks or [], "updated_at": _now()})
        .execute()
    )
    return True


def all_custom_stock_codes():
    """Union of every user's custom-stock codes (for the scheduler's warrant universe)."""
    r = client().table("custom_stocks").select("stocks").execute()
    codes = set()
    for row in (r.data or []):
        for s in (row.get("stocks") or []):
            if isinstance(s, dict) and s.get("code"):
                codes.add(s["code"])
    return list(codes)
