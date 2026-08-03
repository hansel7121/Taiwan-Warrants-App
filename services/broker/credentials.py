"""CRUD for broker_credentials — see supabase/schema.sql and docs/adr/0002.

One row per (user_id, broker); credentials are stored only as a Fernet token in
encrypted_fields. Mirrors db_products.py: thin wrappers, no internal try/except
— callers decide how to handle failures. That is doubly important here: an
except block around the plaintext could put a password into a log line or a
chained traceback, so failures propagate untouched.

get_credential is the only function that produces plaintext (it exists for the
worker's login step); nothing in this module logs its result.
"""
from services import db
from services.broker import crypto


TABLE = "broker_credentials"
CERT_BUCKET = "broker-certs"

# Default capacity tier (symbols_per_connection, connections) per broker.
# KGI's numbers come from manual entry — the broker exposes the account's tier
# nowhere in its API, so 30/2 is only the common base and a caller may override
# it. Fubon's 200/5 are fixed for every account; they are mirrored into the row
# anyway so readers get one uniform shape instead of broker-specific lookups.
# Keep this a table, not an if/elif chain: adding a broker should be one line.
_DEFAULT_TIER = {
    "kgi": (30, 2),
    "fubon": (200, 5),
}

_META_COLS = "broker, symbols_per_connection, connections, cert_path, created_at, updated_at"


def upsert_credential(user_id, broker, fields, symbols_per_connection=None,
                      connections=None):
    """Encrypt `fields` and write the user's row for `broker`.

    Tier args fall back to the broker's default tier, so every row carries a
    capacity tier regardless of broker.
    """
    default_symbols, default_connections = _DEFAULT_TIER.get(broker, (None, None))
    if symbols_per_connection is None:
        symbols_per_connection = default_symbols
    if connections is None:
        connections = default_connections

    row = {
        "user_id": user_id,
        "broker": broker,
        "encrypted_fields": crypto.encrypt(fields),
        "symbols_per_connection": symbols_per_connection,
        "connections": connections,
        "updated_at": db._now(),
    }
    db._run(lambda c: c.table(TABLE).upsert(row, on_conflict="user_id,broker").execute())


def get_credential(user_id, broker):
    """Decrypted credential fields plus the capacity tier columns, or None.

    The return value is plaintext: hand it straight to the broker login call and
    never log, print, or persist it.
    """
    row = _row(user_id, broker)
    if row is None:
        return None
    return {
        **crypto.decrypt(row["encrypted_fields"]),
        "symbols_per_connection": row.get("symbols_per_connection"),
        "connections": row.get("connections"),
    }


def list_credentials(user_id):
    """Metadata only — never decrypts, never returns encrypted_fields. This is
    what a self-service UI renders, so secrets must not reach it."""
    r = db._run(
        lambda c: c.table(TABLE)
        .select(_META_COLS)
        .eq("user_id", user_id)
        .order("broker")
        .execute()
    )
    return [
        {
            "broker": row.get("broker"),
            "symbols_per_connection": row.get("symbols_per_connection"),
            "connections": row.get("connections"),
            "has_cert": bool(row.get("cert_path")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        for row in (r.data or [])
    ]


def remove_credential(user_id, broker):
    # Any uploaded cert object is left in Storage; cleanup belongs with the
    # upload/download flow, out of scope for this phase.
    db._run(
        lambda c: c.table(TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("broker", broker)
        .execute()
    )


def upload_cert(user_id, broker, file_bytes, ext):
    """Store the broker cert in the private broker-certs bucket and record its path.

    The broker segment stays in the path even though only KGI needs a cert
    today — Fubon's .pfx will reuse the same shape.
    """
    path = f"{user_id}/{broker}/cert.{ext}"
    db._run(
        lambda c: c.storage.from_(CERT_BUCKET).upload(
            path, file_bytes, {"upsert": "true"}
        )
    )
    db._run(
        lambda c: c.table(TABLE)
        .update({"cert_path": path, "updated_at": db._now()})
        .eq("user_id", user_id)
        .eq("broker", broker)
        .execute()
    )
    return path


def download_cert(user_id, broker):
    """The stored cert bytes, or None when this row has no cert."""
    row = _row(user_id, broker)
    if row is None or not row.get("cert_path"):
        return None
    return db._run(
        lambda c: c.storage.from_(CERT_BUCKET).download(row["cert_path"]))


def _row(user_id, broker):
    r = db._run(
        lambda c: c.table(TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("broker", broker)
        .limit(1)
        .execute()
    )
    data = r.data or []
    return data[0] if data else None
