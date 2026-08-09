"""Feature -> required-role map, shared by the template gate and the API gate.

Single source of truth for the admin/user split: admin (the builders) sees
everything, user sees only the sellable half — Warrant Scanner, Options
Scanner, IV Surface, live warrant fetch. app.py's index() calls
current_role()/template_flags() to decide which tabs and script tags to emit,
and decorates the admin-only routes with require_role(ADMIN). static/js/
features.js mirrors FEATURES on the browser side.

The role currently comes from a client-settable cookie and require_role is
INERT unless ENFORCE_ROLES=1. That is deliberate: this is a development toggle,
NOT a security boundary. Turning it into a real one means resolving the role
from the authenticated user in current_role() (e.g. a role column on
allowed_users) and setting ENFORCE_ROLES=1 — no other file has to change.
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
    "scanner": USER,      # Warrant Scanner tab
    "options": USER,      # Options Scanner tab — Taiwan side only
    "ivsurface": USER,    # IV Surface tab
    "live": USER,         # live warrant fetch ("Sync now"); reserved for the
                          # broker-fed Live sub-tab on ian/warrant-live-price
    "dashboard": USER,    # user Dashboard tab
    "watchlist": USER,    # starred instruments (user_watchlist)
    "alerts": USER,       # threshold alerts (user_alerts)
    "positions": USER,    # multi-leg positions (user_positions)
    "usoptions": ADMIN,   # US ADR chain — user mode is Taiwan instruments only
    "arb": ADMIN,         # Arb Finder tab + /match_* routes
    "portfolio": ADMIN,   # Portfolio tab + /get_portfolio, /save_portfolio
    "suggestions": ADMIN, # Suggestions sub-tab + /list_suggestions
    "home": ADMIN,        # Home dashboard (runs Direct Match inline)
    "clock": ADMIN,       # market-session clock widget
    "products": ADMIN,    # add/remove tracked products (mutates a shared list)
    "syncUniverse": ADMIN,  # full TWSE re-scrape, 3-5 min
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
