"""The worker's side of the control plane: set_worker_status + the all-users read.

Same faked boundary as test_broker_desired_state.py — db._run only, so no
Supabase request is made.
"""
import pytest

from services import db
from services.broker import desired_state


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


class _Query:
    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = {}

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        return self.table._execute(self)


class _Table:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upsert(self, row, **kw):
        return _Query(self, "upsert", row)

    def select(self, cols="*", **kw):
        return _Query(self, "select", cols)

    def _execute(self, q):
        rows = self.store.rows.setdefault(self.name, [])
        if q.op == "upsert":
            key = (q.payload["user_id"], q.payload["broker"])
            for i, row in enumerate(rows):
                if (row["user_id"], row["broker"]) == key:
                    rows[i] = {**row, **q.payload}
                    return _Result([rows[i]])
            rows.append(dict(q.payload))
            return _Result([q.payload])
        matched = [r for r in rows if all(r.get(k) == v for k, v in q.filters.items())]
        return _Result(matched)


class _Result:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Table(self.store, name)


class _Store:
    def __init__(self):
        self.rows = {}

    def table(self, name):
        return self.rows.setdefault(name, [])


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_Client(s)))
    return s


def test_set_worker_status_writes_one_row_per_account(store):
    desired_state.set_worker_status(USER, "kgi", "connected")
    desired_state.set_worker_status(USER, "fubon", "connected")

    rows = store.table("worker_status")
    assert sorted(r["broker"] for r in rows) == ["fubon", "kgi"]
    assert {r["status"] for r in rows} == {"connected"}
    assert all(r["changed_at"] for r in rows)


def test_transitions_overwrite_rather_than_append(store):
    for status in ["connected", "reconnecting", "disconnected", "stopped"]:
        desired_state.set_worker_status(USER, "kgi", status)

    rows = store.table("worker_status")
    assert len(rows) == 1
    assert rows[0]["status"] == "stopped"


def test_every_documented_state_is_writable(store):
    for status in desired_state.WORKER_STATES:
        desired_state.set_worker_status(USER, "kgi", status)
    assert store.table("worker_status")[0]["status"] == desired_state.WORKER_STATES[-1]


@pytest.mark.parametrize("broker, status", [
    ("etrade", "connected"),
    ("kgi", "connect"),      # the desired-state vocabulary, not the worker's
    ("kgi", "unknown"),
])
def test_unknown_broker_or_status_is_rejected(store, broker, status):
    with pytest.raises(ValueError):
        desired_state.set_worker_status(USER, broker, status)
    assert store.table("worker_status") == []


def test_status_write_never_touches_the_desired_state_table(store):
    desired_state.set_worker_status(USER, "kgi", "connected")
    assert store.table("broker_desired_state") == []


def test_list_all_desired_states_spans_users(store):
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(OTHER, "fubon", "disconnect")

    rows = desired_state.list_all_desired_states()
    assert sorted((r["user_id"], r["desired_state"]) for r in rows) == sorted(
        [(USER, "connect"), (OTHER, "disconnect")])
