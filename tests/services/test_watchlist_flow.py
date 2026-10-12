"""End-to-end watchlist flow against an in-memory stand-in for user_watchlist.

Exercises the real routes — star, list, unstar — and switches the authenticated
user mid-test: user B must never see user A's stars.
"""
import os

import pytest

os.environ.setdefault("LOCAL_USER_ID", "user-a")
os.environ["APP_ENV"] = "local"

import app as app_module  # noqa: E402
from services import db_user  # noqa: E402

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


class FakeStore:
    """Minimal stand-in for user_watchlist, scoped by user_id."""

    def __init__(self):
        self.watch = []
        self.seq = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(db_user, "list_watchlist", self.list_watchlist)
        monkeypatch.setattr(db_user, "add_watchlist", self.add_watchlist)
        monkeypatch.setattr(db_user, "remove_watchlist", self.remove_watchlist)

    def list_watchlist(self, user_id):
        return [r for r in self.watch if r["user_id"] == user_id]

    def add_watchlist(self, user_id, kind, code, underlying_code=None, label=None, meta=None):
        self.remove_watchlist(user_id, kind, code)   # unique (user_id, kind, code)
        self.seq += 1
        self.watch.append({"id": f"w-{self.seq}", "user_id": user_id, "kind": kind,
                           "code": code, "underlying_code": underlying_code,
                           "label": label, "meta": meta or {}})

    def remove_watchlist(self, user_id, kind, code):
        self.watch = [r for r in self.watch
                      if not (r["user_id"] == user_id and r["kind"] == kind and r["code"] == code)]


@pytest.fixture
def env(monkeypatch):
    store = FakeStore()
    store.install(monkeypatch)
    monkeypatch.setenv("LOCAL_USER_ID", USER_A)
    client = app_module.app.test_client()
    return client, store, monkeypatch


def as_user(monkeypatch, user_id):
    monkeypatch.setenv("LOCAL_USER_ID", user_id)


def test_star_then_read_it_back(env):
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
