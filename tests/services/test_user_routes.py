"""Watchlist routes: kind validation and per-user scoping.

user_watchlist is per-user, so a route that took user_id from the request body
would let one user read another's stars.
"""
import os

import pytest

os.environ.setdefault("LOCAL_USER_ID", "test-user-a")
os.environ["APP_ENV"] = "local"

import app as app_module  # noqa: E402  (env must be set before import)
from services import db_user  # noqa: E402

# The repo's .env supplies LOCAL_USER_ID on a dev machine, so read back whatever
# local_mode actually authenticates as rather than assuming the placeholder.
LOCAL_USER = os.environ["LOCAL_USER_ID"]


@pytest.fixture
def client(monkeypatch):
    recorded = {}

    def record(name):
        def fn(user_id, *args, **kwargs):
            recorded[name] = {"user_id": user_id, "args": args, "kwargs": kwargs}
            return []
        return fn

    for name in ("list_watchlist", "add_watchlist", "remove_watchlist"):
        monkeypatch.setattr(db_user, name, record(name))

    c = app_module.app.test_client()
    c.recorded = recorded
    return c


def test_user_id_comes_from_the_session_not_the_body(client):
    """A user_id in the payload must be ignored — otherwise it's an IDOR."""
    client.post("/add_watchlist", json={"user_id": "someone-else", "kind": "warrant", "code": "031100"})
    assert client.recorded["add_watchlist"]["user_id"] == LOCAL_USER


def test_list_watchlist_scopes_to_the_caller(client):
    client.get("/list_watchlist")
    assert client.recorded["list_watchlist"]["user_id"] == LOCAL_USER


def test_watchlist_requires_a_known_kind(client):
    r = client.post("/add_watchlist", json={"kind": "us_option", "code": "X"})
    assert r.status_code == 400
