"""CRUD for fubon_credentials — see supabase/schema.sql.

One row per label (e.g. "default"), so more than one Fubon account/login can
be stored later without a schema change. Credentials are stored only as a
Fernet token in encrypted_fields. Mirrors db_products.py: thin wrappers, no
internal try/except — callers decide how to handle failures. That is doubly
important here: an except block around the plaintext could put a password
into a log line or a chained traceback, so failures propagate untouched.

get_credential is the only function that produces plaintext; nothing in this
module logs its result.
"""
from services import db
from services.broker import crypto


TABLE = "fubon_credentials"
CERT_BUCKET = "broker-certs"

DEFAULT_LABEL = "default"


def upsert_credential(label, fields):
    """Encrypt `fields` (fubon_id, fubon_password, cert_password) and write the row."""
    row = {
        "label": label,
        "encrypted_fields": crypto.encrypt(fields),
        "updated_at": db._now(),
    }
    db._run(lambda c: c.table(TABLE).upsert(row, on_conflict="label").execute())


def get_credential(label=DEFAULT_LABEL):
    """Decrypted credential fields plus cert_path, or None.

    The return value is plaintext: hand it straight to the broker login call and
    never log, print, or persist it.
    """
    row = _row(label)
    if row is None:
        return None
    return {
        **crypto.decrypt(row["encrypted_fields"]),
        "cert_path": row.get("cert_path"),
    }


def list_labels():
    """Metadata only — never decrypts, never returns encrypted_fields."""
    r = db._run(
        lambda c: c.table(TABLE)
        .select("label, cert_path, created_at, updated_at")
        .order("label")
        .execute()
    )
    return [
        {
            "label": row.get("label"),
            "has_cert": bool(row.get("cert_path")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        for row in (r.data or [])
    ]


def remove_credential(label):
    # Any uploaded cert object is left in Storage; cleanup belongs with the
    # upload/download flow, out of scope for this phase.
    db._run(lambda c: c.table(TABLE).delete().eq("label", label).execute())


def upload_cert(label, file_bytes, ext):
    """Store the cert in the private broker-certs bucket and record its path."""
    path = f"fubon/{label}/cert.{ext}"
    db._run(
        lambda c: c.storage.from_(CERT_BUCKET).upload(
            path, file_bytes, {"upsert": "true"}
        )
    )
    db._run(
        lambda c: c.table(TABLE)
        .update({"cert_path": path, "updated_at": db._now()})
        .eq("label", label)
        .execute()
    )
    return path


def download_cert(label=DEFAULT_LABEL):
    """The stored cert bytes, or None when this row has no cert."""
    row = _row(label)
    if row is None or not row.get("cert_path"):
        return None
    return db._run(lambda c: c.storage.from_(CERT_BUCKET).download(row["cert_path"]))


def _row(label):
    r = db._run(
        lambda c: c.table(TABLE).select("*").eq("label", label).limit(1).execute()
    )
    data = r.data or []
    return data[0] if data else None
