"""Feature -> required-role map, shared by the template gate and the API gate.
Single source of truth for the admin/user split; static/js/features.js mirrors
FEATURES on the browser side. The role comes from a client-settable cookie and
require_role is inert unless ENFORCE_ROLES=1 — a dev toggle, not a security
boundary, until current_role() resolves the role from the authenticated user.
"""
import os
from functools import wraps

from flask import request, jsonify

ADMIN = "admin"
USER = "user"

MODE_COOKIE = "app_mode"

# Every gateable feature and the lowest role that may see it. Keep in sync with
# FEATURES in static/js/features.js.
FEATURES = {
    "scanner": USER,
    "options": USER,
    "ivsurface": USER,
    "live": ADMIN,
    "dashboard": USER,
    "watchlist": USER,
    "alerts": USER,
    "positions": USER,
    "usoptions": ADMIN,
    "arb": ADMIN,
    "portfolio": ADMIN,
    "suggestions": ADMIN,
    "home": ADMIN,
    "clock": ADMIN,
    "products": ADMIN,
    "syncUniverse": ADMIN,
}


def current_role():
    """Role for this request. Cookie-based today — see the module docstring."""
    return USER if request.cookies.get(MODE_COOKIE) == USER else ADMIN


def can(feature, role=None):
    """True if `role` (default: this request's) may see `feature`."""
    role = role or current_role()
    return role == ADMIN or FEATURES.get(feature, ADMIN) == USER


def template_flags():
    """Template context for the mode gate: show_admin + the current mode."""
    role = current_role()
    return {"show_admin": role == ADMIN, "app_mode": role}


def _enforcing():
    return os.environ.get("ENFORCE_ROLES") == "1"


def require_role(role):
    """Reject the request when the caller's role is too low. Inert unless ENFORCE_ROLES=1."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if _enforcing() and role == ADMIN and current_role() != ADMIN:
                return jsonify({"error": "forbidden"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
