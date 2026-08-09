"""End-to-end user-dashboard flow against an in-memory stand-in for Supabase.

Exercises the real routes and the real JS-facing payloads — star, list, quote,
build a multi-leg position, alert, then delete — so the only thing separating
this from production is the tables themselves. The isolation test switches the
authenticated user mid-test: user B must never see user A's rows.
"""
import os

import pandas as pd
import pytest

os.environ.setdefault("LOCAL_USER_ID", "user-a")
os.environ.pop("RENDER", None)

import app as app_module  # noqa: E402
from logic import options_logic  # noqa: E402
from logic import warrant_logic  # noqa: E402
from services import db_user  # noqa: E402

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"

WARRANTS = pd.DataFrame([{
    "warrant_code": "031100", "warrant_name": "TSMC C1", "underlying_code": "2330",
    "type": "Call", "underlying_price": 612.0, "ask": 2.10, "bid": 2.05,
    "days_to_expiry": 90, "strike": 600.0, "exercise_ratio": 0.5, "iv_ask": 0.31,
}])

OPTIONS = pd.DataFrame([{
    "contract": "C600 15Aug26", "type": "Call", "underlying_price": 612.0,
    "ask": 18.0, "bid": 17.5, "days_to_expiry": 80, "strike": 600.0,
    "exercise_ratio": 2000, "iv_ask": 0.29, "stock_code": "2330",
}])


class FakeStore:
    """Minimal stand-in for the four user_* tables, scoped by user_id."""

    def __init__(self):
        self.watch, self.alerts, self.positions, self.legs = [], [], [], []
        self.seq = 0

    def _id(self, prefix):
        self.seq += 1
        return f"{prefix}-{self.seq}"

    def install(self, monkeypatch):
        monkeypatch.setattr(db_user, "list_watchlist", self.list_watchlist)
        monkeypatch.setattr(db_user, "add_watchlist", self.add_watchlist)
        monkeypatch.setattr(db_user, "remove_watchlist", self.remove_watchlist)
        monkeypatch.setattr(db_user, "list_alerts", self.list_alerts)
        monkeypatch.setattr(db_user, "add_alert", self.add_alert)
        monkeypatch.setattr(db_user, "remove_alert", self.remove_alert)
        monkeypatch.setattr(db_user, "record_trigger", self.record_trigger)
        monkeypatch.setattr(db_user, "list_positions", self.list_positions)
        monkeypatch.setattr(db_user, "add_position", self.add_position)
        monkeypatch.setattr(db_user, "remove_position", self.remove_position)
        monkeypatch.setattr(db_user, "close_position", self.close_position)

    def list_watchlist(self, user_id):
        return [r for r in self.watch if r["user_id"] == user_id]

    def add_watchlist(self, user_id, kind, code, underlying_code=None, label=None, meta=None):
        self.remove_watchlist(user_id, kind, code)   # unique (user_id, kind, code)
        self.watch.append({"id": self._id("w"), "user_id": user_id, "kind": kind,
                           "code": code, "underlying_code": underlying_code,
                           "label": label, "meta": meta or {}})

    def remove_watchlist(self, user_id, kind, code):
        self.watch = [r for r in self.watch
                      if not (r["user_id"] == user_id and r["kind"] == kind and r["code"] == code)]

    def list_alerts(self, user_id):
        return [r for r in self.alerts if r["user_id"] == user_id]

    def add_alert(self, user_id, kind, code, metric, direction, threshold,
                  underlying_code=None, note=None):
        row = {"id": self._id("a"), "user_id": user_id, "kind": kind, "code": code,
               "underlying_code": underlying_code, "metric": metric,
               "direction": direction, "threshold": threshold, "note": note,
               "active": True, "last_triggered_at": None, "last_value": None}
        self.alerts.append(row)
        return row

    def remove_alert(self, user_id, alert_id):
        self.alerts = [r for r in self.alerts
                       if not (r["user_id"] == user_id and r["id"] == alert_id)]

    def record_trigger(self, user_id, alert_id, value, fired_at):
        for r in self.alerts:
            if r["user_id"] == user_id and r["id"] == alert_id:
                r["last_triggered_at"], r["last_value"] = fired_at, value

    def list_positions(self, user_id):
        out = []
        for p in self.positions:
            if p["user_id"] != user_id:
                continue
            row = dict(p)
            row["legs"] = [l for l in self.legs if l["position_id"] == p["id"]]
            out.append(row)
        return out

    def add_position(self, user_id, legs, name=None, underlying_code=None, note=None):
        pid = self._id("p")
        p = {"id": pid, "user_id": user_id, "name": name,
             "underlying_code": underlying_code, "note": note, "closed_at": None}
        self.positions.append(p)
        for leg in legs:
            self.legs.append({**leg, "id": self._id("l"), "position_id": pid,
                              "user_id": user_id})
        return {**p, "legs": legs}

    def remove_position(self, user_id, position_id):
        self.positions = [p for p in self.positions
                          if not (p["user_id"] == user_id and p["id"] == position_id)]
        self.legs = [l for l in self.legs if l["position_id"] != position_id]

    def close_position(self, user_id, position_id, closed_at):
        for p in self.positions:
            if p["user_id"] == user_id and p["id"] == position_id:
                p["closed_at"] = closed_at


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    store.install(monkeypatch)
    monkeypatch.setattr(warrant_logic, "read_warrant",
                        lambda codes, **kw: (WARRANTS.copy(), None, {}))
    monkeypatch.setattr(options_logic, "read_tw_option",
                        lambda codes, **kw: (OPTIONS.copy(), None, {}))
    monkeypatch.setenv("LOCAL_USER_ID", USER_A)
    client = app_module.app.test_client()
    return client, store, monkeypatch


def as_user(monkeypatch, user_id):
    monkeypatch.setenv("LOCAL_USER_ID", user_id)


def test_star_then_read_it_back_on_the_dashboard(env):
    client, _store, _mp = env
    r = client.post("/add_watchlist", json={
        "kind": "warrant", "code": "031100", "underlying_code": "2330",
        "label": "TSMC C1", "meta": {"strike": 600, "exercise_ratio": 0.5}})
    assert r.status_code == 200, r.get_json()

    rows = client.get("/list_watchlist").get_json()
    assert [x["code"] for x in rows] == ["031100"]
    assert rows[0]["label"] == "TSMC C1"


def test_starring_twice_does_not_duplicate(env):
    client, _store, _mp = env
    body = {"kind": "warrant", "code": "031100", "underlying_code": "2330"}
    client.post("/add_watchlist", json=body)
    client.post("/add_watchlist", json=body)
    assert len(client.get("/list_watchlist").get_json()) == 1


def test_unstar_removes_it(env):
    client, _store, _mp = env
    body = {"kind": "warrant", "code": "031100", "underlying_code": "2330"}
    client.post("/add_watchlist", json=body)
    client.post("/remove_watchlist", json=body)
    assert client.get("/list_watchlist").get_json() == []


def test_watchlist_is_private_to_each_user(env):
    client, _store, mp = env
    client.post("/add_watchlist", json={"kind": "warrant", "code": "031100",
                                        "underlying_code": "2330"})
    as_user(mp, USER_B)
    assert client.get("/list_watchlist").get_json() == []
    client.post("/add_watchlist", json={"kind": "tw_option", "code": "C600 15Aug26",
                                        "underlying_code": "2330"})
    assert [x["code"] for x in client.get("/list_watchlist").get_json()] == ["C600 15Aug26"]

    as_user(mp, USER_A)
    assert [x["code"] for x in client.get("/list_watchlist").get_json()] == ["031100"]


def test_quotes_for_a_starred_warrant_and_option(env):
    client, _store, _mp = env
    data = client.post("/user_quotes", json={"instruments": [
        {"kind": "warrant", "code": "031100", "underlying_code": "2330"},
        {"kind": "tw_option", "code": "C600 15Aug26", "underlying_code": "2330"},
        {"kind": "underlying", "code": "2330", "underlying_code": "2330"},
    ]}).get_json()["quotes"]
    assert data["warrant:031100"]["bid"] == 2.05
    assert data["tw_option:C600 15Aug26"]["ask"] == 18.0
    assert data["underlying:2330"]["underlying_price"] == 612.0


def test_multi_leg_position_round_trip(env):
    client, _store, _mp = env
    legs = [
        {"kind": "warrant", "code": "031100", "quantity": 3, "entry_price": 2.0,
         "direction": 1, "option_type": "Call", "strike": 600,
         "days_to_expiry": 90, "exercise_ratio": 0.5},
        {"kind": "tw_option", "code": "C600 15Aug26", "quantity": 1,
         "entry_price": 18.0, "direction": -1, "option_type": "Call",
         "strike": 600, "days_to_expiry": 80, "contract_size": 2000},
        {"kind": "underlying", "code": "2330", "quantity": 500,
         "entry_price": 610.0, "direction": -1},
    ]
    r = client.post("/add_position", json={"name": "covered", "underlying_code": "2330",
                                           "legs": legs})
    assert r.status_code == 200, r.get_json()

    positions = client.get("/list_positions").get_json()
    assert len(positions) == 1
    got = positions[0]
    assert got["name"] == "covered"
    assert len(got["legs"]) == 3
    assert {l["kind"] for l in got["legs"]} == {"warrant", "tw_option", "underlying"}
    # Every field the payoff modal reads must survive the round trip.
    warrant_leg = next(l for l in got["legs"] if l["kind"] == "warrant")
    for field in ("quantity", "entry_price", "direction", "strike",
                  "days_to_expiry", "exercise_ratio", "option_type"):
        assert warrant_leg[field] is not None, field


def test_positions_are_private_to_each_user(env):
    client, _store, mp = env
    client.post("/add_position", json={"legs": [
        {"kind": "warrant", "code": "031100", "quantity": 1, "entry_price": 2.0}]})
    as_user(mp, USER_B)
    assert client.get("/list_positions").get_json() == []


def test_close_then_delete_a_position(env):
    client, _store, _mp = env
    pid = client.post("/add_position", json={"legs": [
        {"kind": "warrant", "code": "031100", "quantity": 1,
         "entry_price": 2.0}]}).get_json()["position"]["id"]

    assert client.post("/close_position", json={"id": pid}).status_code == 200
    assert client.get("/list_positions").get_json()[0]["closed_at"] is not None

    assert client.post("/remove_position", json={"id": pid}).status_code == 200
    assert client.get("/list_positions").get_json() == []


def test_alert_lifecycle_and_trigger_record(env):
    client, store, _mp = env
    created = client.post("/add_alert", json={
        "kind": "warrant", "code": "031100", "underlying_code": "2330",
        "metric": "bid", "direction": "above", "threshold": 2.0}).get_json()["alert"]

    assert client.get("/list_alerts").get_json()[0]["threshold"] == 2.0

    client.post("/record_alert_trigger", json={"id": created["id"], "value": 2.05})
    stored = client.get("/list_alerts").get_json()[0]
    assert stored["last_value"] == 2.05 and stored["last_triggered_at"]

    client.post("/remove_alert", json={"id": created["id"]})
    assert client.get("/list_alerts").get_json() == []


def test_alerts_are_private_to_each_user(env):
    client, _store, mp = env
    client.post("/add_alert", json={"kind": "warrant", "code": "031100",
                                    "metric": "bid", "direction": "above",
                                    "threshold": 2.0})
    as_user(mp, USER_B)
    assert client.get("/list_alerts").get_json() == []
