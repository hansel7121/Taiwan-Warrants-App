"""Supabase Postgres access (service-role) for per-user portfolio / custom stocks.

The client is created lazily so the module imports fine without any env vars
(local no-auth dev, sanity checks). A tiny .env loader runs at import so
SUPABASE_* vars in a local .env are visible to this module and auth.py.
"""
import os
from datetime import datetime, timezone


def _load_dotenv():
    """Minimal .env loader (python-dotenv is not a dependency). Silent on any error."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_portfolio(user_id):
    r = client().table("portfolio").select("payload").eq("user_id", user_id).execute()
    return [row["payload"] for row in (r.data or [])]


def save_portfolio(user_id, entries):
    """Diff-upsert: upsert every posted entry, then delete rows no longer present."""
    c = client()
    ids, rows = [], []
    now = _now()
    for e in entries or []:
        eid = str(e.get("id"))
        ids.append(eid)
        rows.append({"user_id": user_id, "id": eid, "payload": e, "updated_at": now})
    if rows:
        c.table("portfolio").upsert(rows).execute()
    q = c.table("portfolio").delete().eq("user_id", user_id)
    if ids:
        q = q.not_.in_("id", ids)
    q.execute()
    return True


def get_custom_stocks(user_id):
    r = client().table("custom_stocks").select("stocks").eq("user_id", user_id).execute()
    if r.data:
        return r.data[0].get("stocks") or []
    return []


def save_custom_stocks(user_id, stocks):
    client().table("custom_stocks").upsert(
        {"user_id": user_id, "stocks": stocks or [], "updated_at": _now()}
    ).execute()
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
