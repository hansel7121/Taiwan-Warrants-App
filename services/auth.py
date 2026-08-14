"""Supabase magic-link auth: JWT verification + email allowlist.

require_auth guards API routes. Verifies the Bearer token (RS256/ES256 via the
project's JWKS, HS256 fallback with SUPABASE_JWT_SECRET), then checks the token
email against the allowed_users table (cached 60s per email). The authenticated
user (id + email) is exposed on flask.g.user.

Imports cleanly with no env: require_auth returns 503 when SUPABASE_URL is unset
so the app still starts for local no-auth dev / sanity checks.
"""
import os
import time
from functools import wraps

import jwt
from jwt import PyJWKClient
from flask import request, jsonify, g

from services import db

_jwks_client = None
_allow_cache = {}  # email -> (expiry_epoch, bool)
_ALLOW_TTL = 60

# PyJWKClient defaults to a 30s network timeout; on a single gthread-worker
# process a slow/flaky Supabase auth backend could pin a request thread for
# up to 30s per hit.
_JWKS_TIMEOUT_SEC = float(os.environ.get("JWKS_TIMEOUT_SEC", "5"))


def local_mode():
    """True on a local redundancy instance that acts as a fixed user with no login.

    Enabled only when LOCAL_USER_ID is set AND we are not on Render. Computed at
    call time (not import) so tests can toggle env. On Render, RENDER is set so
    this is always False and production auth is untouched.
    """
    return bool(os.environ.get("LOCAL_USER_ID")) and not os.environ.get("RENDER")


def public_config():
    """Values injected into templates for the browser-side Supabase client."""
    return {
        "supabase_url": os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
        "local_mode": local_mode(),
    }


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        url = os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(url, timeout=_JWKS_TIMEOUT_SEC)
    return _jwks_client


def _verify(token):
    """Verify and decode the JWT. Primary: asymmetric via JWKS; fallback: HS256."""
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        return jwt.decode(
            token, signing_key.key,
            algorithms=["RS256", "ES256"], audience="authenticated",
        )
    except Exception:
        secret = os.environ.get("SUPABASE_JWT_SECRET")
        if secret:
            return jwt.decode(
                token, secret, algorithms=["HS256"], audience="authenticated",
            )
        raise


def _is_allowed(email):
    if not email:
        return False
    now = time.time()
    hit = _allow_cache.get(email)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = db.run(lambda c: c.table("allowed_users").select("email").eq("email", email).execute())
    except Exception as e:
        # A transient Supabase hiccup is not proof the user isn't allowed -
        # don't cache it, or one blip locks a real user out for _ALLOW_TTL.
        print(f"AUTH: allowlist check failed for {email}: {e}", flush=True)
        return False
    ok = bool(r.data)
    _allow_cache[email] = (now + _ALLOW_TTL, ok)
    return ok


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if local_mode():
            # Local redundancy instance: act as the fixed LOCAL_USER_ID, skipping
            # token verification and the allowlist entirely. Guarded by RENDER so
            # this branch is dead on production.
            g.user = {
                "id": os.environ["LOCAL_USER_ID"],
                "email": os.environ.get("LOCAL_USER_EMAIL", "local@localhost"),
            }
            return fn(*args, **kwargs)
        if not os.environ.get("SUPABASE_URL"):
            return jsonify({"error": "auth not configured"}), 503
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "missing_token"}), 401
        token = header[7:].strip()
        try:
            claims = _verify(token)
        except Exception:
            return jsonify({"error": "invalid_token"}), 401
        email = claims.get("email")
        if not _is_allowed(email):
            return jsonify({"error": "not_allowed"}), 403
        g.user = {"id": claims.get("sub"), "email": email}
        return fn(*args, **kwargs)
    return wrapper
