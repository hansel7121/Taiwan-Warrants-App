"""Shared Watchlist tests, service layer and routes.

Real in-process calls; the only faked boundary is db._run / the supabase
client, so no Supabase request is ever made. The fakes are local rather than
reused from test_broker_desired_state.py because this module spans TWO tables
for a different reason: watchlist is the data, broker_credentials is what
decides how much of it fits, and the coupling between them (capacity) is most
of what these tests exist to pin down.

Capacity Tiers are seeded straight into the fake broker_credentials rows with
small numbers (2 symbols x 1 connection) — the real kgi/fubon defaults are
60 and 1000, which no readable test wants to fill.

Auth uses the app's existing local_mode path (LOCAL_USER_ID + no RENDER), the
same way test_broker_desired_state.py does; the Watchlist is shared, so
swapping LOCAL_USER_ID is how "another user sees the same list" is expressed.
"""
import pytest

from services import db
from services.broker import watchlist


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


class _FakeQuery:
    """Chainable stand-in for a supabase-py table query; records what it did."""

    def __init__(self, table, op, payload=None, on_conflict=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.on_conflict = on_conflict
        self.filters = {}
        self.in_filters = {}

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, col, vals):
        self.in_filters[col] = list(vals)
        return self

    def order(self, col, **kw):
        self.order_by = col
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        return self.table._execute(self)


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upsert(self, row, on_conflict=None, **kw):
        return _FakeQuery(self, "upsert", row, on_conflict)

    def select(self, cols="*", **kw):
        return _FakeQuery(self, "select", cols)

    def delete(self, **kw):
        return _FakeQuery(self, "delete")

    def _rows(self):
        return self.store.rows.setdefault(self.name, [])

    def _match(self, row, q):
        return (all(row.get(k) == v for k, v in q.filters.items())
                and all(row.get(k) in v for k, v in q.in_filters.items()))

    def _execute(self, q):
        self.store.ops.append((self.name, q.op, q.payload, dict(q.filters)))
        rows = self._rows()
        if q.op == "upsert":
            payload = q.payload if isinstance(q.payload, list) else [q.payload]
            key_cols = (q.on_conflict or "user_id,broker").split(",")
            written = []
            for row in payload:
                key = tuple(row[c] for c in key_cols)
                for i, existing in enumerate(rows):
                    if tuple(existing.get(c) for c in key_cols) == key:
                        rows[i] = {**existing, **row}
                        written.append(rows[i])
                        break
                else:
                    rows.append(dict(row))
                    written.append(rows[-1])
            return _Result(written)
        if q.op == "delete":
            gone = [r for r in rows if self._match(r, q)]
            rows[:] = [r for r in rows if not self._match(r, q)]
            return _Result(gone)
        got = [r for r in rows if self._match(r, q)]
        order_by = getattr(q, "order_by", None)
        if order_by:
            got = sorted(got, key=lambda r: r.get(order_by) or "")
        return _Result(got)


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

    def credit(self, user_id, broker="kgi", symbols_per_connection=2, connections=1):
        """Give `user_id` a Broker Account, i.e. capacity for the shared pool."""
        self.table("broker_credentials").append({
            "user_id": user_id,
            "broker": broker,
            "encrypted_fields": "x",
            "symbols_per_connection": symbols_per_connection,
            "connections": connections,
        })

    def codes(self):
        return sorted(r["code"] for r in self.table("watchlist"))


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    return s


# ── adding ───────────────────────────────────────────────────────────────

def test_add_within_capacity_writes_the_codes(store):
    store.credit(USER)

    result = watchlist.add_codes(USER, ["031100", "031101"])

    assert result.added == ["031100", "031101"]
    assert result.rejected == []
    assert store.codes() == ["031100", "031101"]


def test_the_adding_user_is_recorded_but_grants_no_ownership(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["031100"])

    row = store.table("watchlist")[0]
    assert row["added_by"] == USER
    assert row["added_at"]


def test_capacity_is_the_sum_over_every_users_broker_accounts(store):
    """One shared pool (docs/adr/0001): another user's account adds room."""
    store.credit(USER)
    store.credit(OTHER)

    result = watchlist.add_codes(USER, ["A", "B", "C", "D"])

    assert result.rejected == []
    assert result.capacity == 4
    assert store.codes() == ["A", "B", "C", "D"]


def test_an_overflowing_batch_is_rejected_whole_and_writes_nothing(store):
    """No partial accept, no eviction (docs/adr/0002)."""
    store.credit(USER)

    result = watchlist.add_codes(USER, ["A", "B", "C"])

    assert result.added == []
    assert result.rejected == ["A", "B", "C"]
    assert store.table("watchlist") == []


def test_a_batch_that_only_partly_fits_still_writes_nothing(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["A"])

    result = watchlist.add_codes(USER, ["B", "C"])

    assert result.rejected == ["B", "C"]
    assert store.codes() == ["A"]


def test_with_no_broker_account_there_is_no_capacity_at_all(store):
    result = watchlist.add_codes(USER, ["A"])

    assert result.capacity == 0
    assert result.rejected == ["A"]
    assert store.table("watchlist") == []


def test_a_code_already_watched_is_not_written_twice(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["031100"])

    result = watchlist.add_codes(OTHER, ["031100"])

    assert result.added == []
    assert result.rejected == []
    rows = store.table("watchlist")
    assert len(rows) == 1
    assert rows[0]["added_by"] == USER


def test_re_adding_a_watched_code_does_not_consume_capacity(store):
    """Otherwise a full list could never be re-confirmed by a second user."""
    store.credit(USER)
    watchlist.add_codes(USER, ["A", "B"])

    result = watchlist.add_codes(OTHER, ["A", "B"])

    assert result.rejected == []
    assert store.codes() == ["A", "B"]


def test_a_batch_repeating_one_code_counts_it_once(store):
    store.credit(USER)

    result = watchlist.add_codes(USER, ["A", "A", "B"])

    assert result.added == ["A", "B"]
    assert store.codes() == ["A", "B"]


def test_codes_are_stripped_but_not_case_folded(store):
    store.credit(USER)

    watchlist.add_codes(USER, ["  031100 ", "", "  "])

    assert store.codes() == ["031100"]


# ── removing ─────────────────────────────────────────────────────────────

def test_remove_returns_the_codes_that_were_on_the_list(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["A", "B"])

    assert watchlist.remove_codes([" A "]) == ["A"]
    assert store.codes() == ["B"]


def test_removing_an_unwatched_code_is_a_no_op(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["A"])

    assert watchlist.remove_codes(["ZZZ"]) == []
    assert store.codes() == ["A"]


def test_removing_frees_capacity_for_the_next_add(store):
    store.credit(USER)
    watchlist.add_codes(USER, ["A", "B"])
    assert watchlist.add_codes(USER, ["C"]).rejected == ["C"]

    watchlist.remove_codes(["A"])

    assert watchlist.add_codes(USER, ["C"]).added == ["C"]
    assert store.codes() == ["B", "C"]


def test_anyone_can_remove_a_code_somebody_else_added(store):
    """The list is shared, so removal is not scoped to the adder."""
    store.credit(USER)
    watchlist.add_codes(USER, ["A"])

    assert watchlist.remove_codes(["A"]) == ["A"]
    assert store.table("watchlist") == []


# ── listing ──────────────────────────────────────────────────────────────

def test_list_is_shared_rather_than_filtered_by_user(store):
    store.credit(USER)
    store.credit(OTHER)
    watchlist.add_codes(USER, ["A"])
    watchlist.add_codes(OTHER, ["B"])

    assert watchlist.list_codes() == ["A", "B"]


def test_list_is_empty_before_anything_is_added(store):
    assert watchlist.list_codes() == []


# ── routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    import app as app_module

    app_module.app.config["TESTING"] = True
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", USER)
    with app_module.app.test_client() as c:
        yield c


def test_add_route_returns_the_added_codes(client, store):
    store.credit(USER)

    r = client.post("/watchlist/add", json={"codes": ["031100", "031101"]})

    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "codes": ["031100", "031101"]}
    assert store.codes() == ["031100", "031101"]


def test_add_route_answers_400_when_the_pool_is_full(client, store):
    """A full pool is an ordinary answer, never a 500."""
    store.credit(USER)

    r = client.post("/watchlist/add", json={"codes": ["A", "B", "C"]})

    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "capacity" in body["error"]
    assert store.table("watchlist") == []


@pytest.mark.parametrize("body", [{}, {"codes": []}, {"codes": "031100"}, {"codes": None}])
def test_add_route_rejects_a_body_that_is_not_a_code_list(client, store, body):
    store.credit(USER)

    r = client.post("/watchlist/add", json=body)

    assert r.status_code == 400
    assert "error" in r.get_json()
    assert store.table("watchlist") == []


def test_remove_route_returns_what_it_removed(client, store):
    store.credit(USER)
    client.post("/watchlist/add", json={"codes": ["A", "B"]})

    r = client.post("/watchlist/remove", json={"codes": ["A", "ZZZ"]})

    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "codes": ["A"]}
    assert store.codes() == ["B"]


def test_list_route_shows_every_users_codes(client, store, monkeypatch):
    store.credit(USER)
    store.credit(OTHER)
    client.post("/watchlist/add", json={"codes": ["A"]})
    monkeypatch.setenv("LOCAL_USER_ID", OTHER)
    client.post("/watchlist/add", json={"codes": ["B"]})

    r = client.get("/watchlist/list")

    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "codes": ["A", "B"]}
