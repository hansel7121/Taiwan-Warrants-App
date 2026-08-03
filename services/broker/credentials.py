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

# KGI's base market-data tier, entered manually because the broker exposes it
# nowhere in the API. Fubon's limits are fixed constants elsewhere, not stored.
KGI_BASE_SYMBOLS_PER_CONNECTION = 30
KGI_BASE_CONNECTIONS = 2

_META_COLS = "broker, kgi_symbols_per_connection, kgi_connections, cert_path, created_at, updated_at"


def upsert_credential(user_id, broker, fields, kgi_symbols_per_connection=None,
                      kgi_connections=None):
    """Encrypt `fields` and write the user's row for `broker`.

    KGI's tier args default to the base tier; they stay null for fubon, where
    the columns are meaningless.
    """
    if broker == "kgi":
        if kgi_symbols_per_connection is None:
            kgi_symbols_per_connection = KGI_BASE_SYMBOLS_PER_CONNECTION
        if kgi_connections is None:
            kgi_connections = KGI_BASE_CONNECTIONS

    row = {
        "user_id": user_id,
        "broker": broker,
        "encrypted_fields": crypto.encrypt(fields),
        "kgi_symbols_per_connection": kgi_symbols_per_connection,
        "kgi_connections": kgi_connections,
        "updated_at": db._now(),
    }
    db._run(lambda c: c.table(TABLE).upsert(row, on_conflict="user_id,broker").execute())


def get_credential(user_id, broker):
    """Decrypted credential fields plus the KGI tier columns, or None.

    The return value is plaintext: hand it straight to the broker login call and
    never log, print, or persist it.
    """
    row = _row(user_id, broker)
    if row is None:
        return None
    return {
        **crypto.decrypt(row["encrypted_fields"]),
        "kgi_symbols_per_connection": row.get("kgi_symbols_per_connection"),
        "kgi_connections": row.get("kgi_connections"),
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
            "kgi_symbols_per_connection": row.get("kgi_symbols_per_connection"),
            "kgi_connections": row.get("kgi_connections"),
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
