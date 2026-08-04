"""Fernet encryption for broker credentials at rest (docs/adr/0002).

The key is read lazily, mirroring db.py's lazy client, so the module imports
fine without BROKER_CRED_KEY set (local dev, sanity checks, unrelated tests).
Nothing here logs or prints: a stack trace or debug line carrying a broker
password is exactly the leak this table exists to prevent, so this module has
no print/logging calls at all and never wraps a value in an exception message.
"""
import json
import os


_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        key = os.environ.get("BROKER_CRED_KEY")
        if not key:
            raise RuntimeError("BROKER_CRED_KEY not configured")
        from cryptography.fernet import Fernet
        _fernet = Fernet(key)
    return _fernet


def _reset_fernet():
    """Drop the cached Fernet so the next call re-reads BROKER_CRED_KEY."""
    global _fernet
    _fernet = None


def encrypt(fields):
    """Serialize a credential dict and return a Fernet token as str.

    str (not bytes) because the column is postgres `text`.
    """
    return _get_fernet().encrypt(json.dumps(fields).encode()).decode()


def decrypt(token):
    """Inverse of encrypt(). The returned plaintext must never be logged."""
    return json.loads(_get_fernet().decrypt(token.encode()).decode())
