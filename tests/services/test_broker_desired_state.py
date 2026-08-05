"""broker_desired_state / worker_status tests, service layer and routes.

Real in-process calls; the only faked boundary is db._run / the supabase client,
so no Supabase request is ever made. The fakes are local rather than reused from
test_broker_credentials.py because this module spans TWO tables, and the
separation between them — routes write one, the worker writes the other — is
most of what these tests exist to pin down.

Auth uses the app's existing local_mode path (LOCAL_USER_ID + no RENDER), the
same way test_broker_credential_routes.py does; swapping LOCAL_USER_ID between
requests is how the "another user is independent" case is expressed.
"""
import pytest

from services import db
from services.broker import desired_state


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


class _FakeQuery:
    """Chainable stand-in for a supabase-py table query; records what it did."""

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


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upsert(self, row, **kw):
        return _FakeQuery(self, "upsert", row)

    def select(self, cols="*", **kw):
        return _FakeQuery(self, "select", cols)

    def delete(self, **kw):
        return _FakeQuery(self, "delete")

    def _rows(self):
        return self.store.rows.setdefault(self.name, [])

    def _match(self, row, q):
        return all(row.get(k) == v for k, v in q.filters.items())

    def _execute(self, q):
        self.store.ops.append((self.name, q.op, q.payload, dict(q.filters)))
        rows = self._rows()
        if q.op == "upsert":
            key = (q.payload["user_id"], q.payload["broker"])
            for i, row in enumerate(rows):
                if (row["user_id"], row["broker"]) == key:
                    rows[i] = {**row, **q.payload}
                    return _Result([rows[i]])
            rows.append(dict(q.payload))
            return _Result([q.payload])
        if q.op == "delete":
            gone = [r for r in rows if self._match(r, q)]
            rows[:] = [r for r in rows if not self._match(r, q)]
            return _Result(gone)
        return _Result([r for r in rows if self._match(r, q)])


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        self.store.tables.append(name)
        return _FakeTable(self.store, name)


class _Store:
    """Table name -> rows, so a test can assert one table stayed untouched."""

    def __init__(self):
        self.rows = {}
        self.ops = []
        self.tables = []

    def table(self, name):
        return self.rows.setdefault(name, [])


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    return s


# ── desired state ────────────────────────────────────────────────────────

def test_set_desired_state_writes_the_users_row(store):
    desired_state.set_desired_state(USER, "kgi", "connect")

    assert store.tables == ["broker_desired_state"]
    row = store.table("broker_desired_state")[0]
    assert row["user_id"] == USER
    assert row["broker"] == "kgi"
    assert row["desired_state"] == "connect"
    assert row["requested_at"]


def test_latest_request_wins_rather_than_queueing(store):
    for state in ["connect", "disconnect", "connect", "disconnect"]:
        desired_state.set_desired_state(USER, "kgi", state)

    rows = store.table("broker_desired_state")
    assert len(rows) == 1
    assert rows[0]["desired_state"] == "disconnect"


def test_rows_are_keyed_on_user_and_broker(store):
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(USER, "fubon", "connect")
    desired_state.set_desired_state(OTHER, "kgi", "disconnect")

    rows = store.table("broker_desired_state")
    assert sorted((r["user_id"], r["broker"]) for r in rows) == sorted(
        [(USER, "kgi"), (USER, "fubon"), (OTHER, "kgi")])


def test_get_desired_state_is_scoped_to_the_user(store):
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(OTHER, "kgi", "disconnect")

    assert desired_state.get_desired_state(USER, "kgi")["desired_state"] == "connect"
    assert desired_state.get_desired_state(OTHER, "kgi")["desired_state"] == "disconnect"
    assert desired_state.get_desired_state(USER, "fubon") is None


def test_list_desired_states_returns_only_this_users_rows(store):
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(USER, "fubon", "disconnect")
    desired_state.set_desired_state(OTHER, "kgi", "connect")

    listed = desired_state.list_desired_states(USER)
    assert sorted(r["broker"] for r in listed) == ["fubon", "kgi"]


def test_list_all_desired_states_spans_every_user(store):
    """The worker's read: one process serves everyone, so it filters on nobody."""
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(OTHER, "fubon", "connect")

    rows = desired_state.list_all_desired_states()
    assert sorted((r["user_id"], r["broker"]) for r in rows) == sorted(
        [(USER, "kgi"), (OTHER, "fubon")])


@pytest.mark.parametrize("broker, state", [
    ("etrade", "connect"),
    ("kgi", "reconnect"),
    ("", "connect"),
])
def test_unknown_broker_or_state_is_rejected(store, broker, state):
    with pytest.raises(ValueError):
        desired_state.set_desired_state(USER, broker, state)
    assert store.table("broker_desired_state") == []


# ── worker status ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", desired_state.WORKER_STATES)
def test_every_worker_state_can_be_reported(store, status):
    row = desired_state.set_worker_status(USER, "kgi", status)

    assert row["status"] == status
    assert store.table("worker_status")[0]["status"] == status
    assert store.table("worker_status")[0]["changed_at"]


def test_worker_status_upserts_rather_than_logging_transitions(store):
    for status in ("connected", "reconnecting", "disconnected", "stopped"):
        desired_state.set_worker_status(USER, "kgi", status)

    rows = store.table("worker_status")
    assert len(rows) == 1
    assert rows[0]["status"] == "stopped"


@pytest.mark.parametrize("broker, status", [
    ("etrade", "connected"),
    ("kgi", "connecting"),
])
def test_unknown_broker_or_worker_status_is_rejected(store, broker, status):
    with pytest.raises(ValueError):
        desired_state.set_worker_status(USER, broker, status)
    assert store.table("worker_status") == []


def test_list_worker_status_is_empty_until_a_worker_writes(store):
    assert desired_state.list_worker_status(USER) == []


def test_list_worker_status_is_scoped_to_the_user(store):
    desired_state.set_worker_status(USER, "kgi", "connected")
    desired_state.set_worker_status(OTHER, "kgi", "stopped")

    rows = desired_state.list_worker_status(USER)
    assert [(r["broker"], r["status"]) for r in rows] == [("kgi", "connected")]


def test_setting_desired_state_never_touches_worker_status(store):
    desired_state.set_desired_state(USER, "kgi", "connect")
    desired_state.set_desired_state(USER, "kgi", "disconnect")

    assert "worker_status" not in store.tables
    assert store.table("worker_status") == []


# ── routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    import app as app_module

    app_module.app.config["TESTING"] = True
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", USER)
    with app_module.app.test_client() as c:
        yield c


@pytest.mark.parametrize("route, expected", [
    ("/broker/connect", "connect"),
    ("/broker/disconnect", "disconnect"),
])
def test_route_upserts_the_callers_row(client, store, route, expected):
    r = client.post(route, json={"broker": "kgi"})

    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "broker": "kgi", "desired_state": expected}
    rows = store.table("broker_desired_state")
    assert len(rows) == 1
    assert rows[0]["user_id"] == USER
    assert rows[0]["broker"] == "kgi"
    assert rows[0]["desired_state"] == expected


def test_connect_then_disconnect_leaves_one_row(client, store):
    client.post("/broker/connect", json={"broker": "fubon"})
    client.post("/broker/disconnect", json={"broker": "fubon"})

    rows = store.table("broker_desired_state")
    assert len(rows) == 1
    assert rows[0]["desired_state"] == "disconnect"


def test_routes_never_write_worker_status(client, store):
    """Only the worker may author a status row, or the UI is reading a wish."""
    client.post("/broker/connect", json={"broker": "kgi"})

    assert store.table("worker_status") == []


@pytest.mark.parametrize("body", [{}, {"broker": "etrade"}, {"broker": None}])
def test_route_rejects_an_unknown_broker(client, store, body):
    r = client.post("/broker/connect", json=body)

    assert r.status_code == 400
    assert "error" in r.get_json()
    assert store.table("broker_desired_state") == []


def test_broker_name_is_normalised_like_the_credential_routes(client, store):
    r = client.post("/broker/connect", json={"broker": "  KGI "})

    assert r.status_code == 200
    assert store.table("broker_desired_state")[0]["broker"] == "kgi"


def test_a_click_cannot_move_another_users_session(client, store, monkeypatch):
    client.post("/broker/connect", json={"broker": "kgi"})

    monkeypatch.setenv("LOCAL_USER_ID", OTHER)
    client.post("/broker/disconnect", json={"broker": "kgi"})

    rows = sorted(store.table("broker_desired_state"), key=lambda r: r["user_id"])
    assert [(r["user_id"], r["desired_state"]) for r in rows] == [
        (USER, "connect"), (OTHER, "disconnect")]
