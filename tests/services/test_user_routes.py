"""User dashboard routes: leg validation and per-user scoping.

The scoping tests are the important ones — every one of these tables is
per-user, so a route that took user_id from the request body would let one user
read another's positions.
"""
import os

import pytest

os.environ.setdefault("LOCAL_USER_ID", "test-user-a")
os.environ.pop("RENDER", None)

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
            if name == "add_position":
                return {"id": "p1", "legs": args[0]}
            if name == "add_alert":
                return {"id": "a1"}
            return []
        return fn

    for name in ("list_watchlist", "add_watchlist", "remove_watchlist",
                 "list_alerts", "add_alert", "remove_alert", "record_trigger",
                 "list_positions", "add_position", "remove_position", "close_position"):
        monkeypatch.setattr(db_user, name, record(name))

    c = app_module.app.test_client()
    c.recorded = recorded
    return c


LEG = {"kind": "warrant", "code": "031100", "quantity": 2,
       "entry_price": 1.5, "direction": 1}


def test_position_needs_at_least_one_leg(client):
    r = client.post("/add_position", json={"legs": []})
    assert r.status_code == 400


def test_warrants_cannot_be_shorted(client):
    r = client.post("/add_position", json={"legs": [{**LEG, "direction": -1}]})
    assert r.status_code == 400
    assert "long-only" in r.get_json()["error"]


def test_unknown_leg_kind_rejected(client):
    r = client.post("/add_position", json={"legs": [{**LEG, "kind": "us_option"}]})
    assert r.status_code == 400


def test_non_positive_quantity_rejected(client):
    r = client.post("/add_position", json={"legs": [{**LEG, "quantity": 0}]})
    assert r.status_code == 400


def test_non_numeric_entry_price_rejected(client):
    r = client.post("/add_position", json={"legs": [{**LEG, "entry_price": "abc"}]})
    assert r.status_code == 400


def test_multi_leg_position_accepted(client):
    legs = [
        LEG,
        {"kind": "tw_option", "code": "C600 15Aug26", "quantity": 1,
         "entry_price": 12.5, "direction": -1, "option_type": "Call",
         "strike": 600, "days_to_expiry": 80, "contract_size": 2000},
        {"kind": "underlying", "code": "2330", "quantity": 1000,
         "entry_price": 612, "direction": -1},
    ]
    r = client.post("/add_position", json={"name": "hedge", "legs": legs})
    assert r.status_code == 200
    assert len(r.get_json()["position"]["legs"]) == 3


def test_bogus_option_type_is_dropped_not_stored(client):
    r = client.post("/add_position", json={"legs": [{**LEG, "option_type": "Straddle"}]})
    assert r.status_code == 200
    assert client.recorded["add_position"]["args"][0][0]["option_type"] is None


def test_user_id_comes_from_the_session_not_the_body(client):
    """A user_id in the payload must be ignored — otherwise it's an IDOR."""
    client.post("/add_position", json={"user_id": "someone-else", "legs": [LEG]})
    assert client.recorded["add_position"]["user_id"] == LOCAL_USER


@pytest.mark.parametrize("path", ["/list_watchlist", "/list_alerts", "/list_positions"])
def test_list_routes_scope_to_the_caller(client, path):
    client.get(path)
    name = path.lstrip("/")
    assert client.recorded[name]["user_id"] == LOCAL_USER


def test_alert_rejects_unknown_metric(client):
    r = client.post("/add_alert", json={"kind": "warrant", "code": "031100",
                                        "metric": "vega", "direction": "above",
                                        "threshold": 1})
    assert r.status_code == 400


def test_alert_rejects_non_numeric_threshold(client):
    r = client.post("/add_alert", json={"kind": "warrant", "code": "031100",
                                        "metric": "iv", "direction": "above",
                                        "threshold": "high"})
    assert r.status_code == 400


def test_watchlist_requires_a_known_kind(client):
    r = client.post("/add_watchlist", json={"kind": "us_option", "code": "X"})
    assert r.status_code == 400
