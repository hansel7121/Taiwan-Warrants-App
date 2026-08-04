"""arb_signals writes: the row shape a matcher result becomes, and the
append-only lifecycle that distinguishes it from arb_suggestions (docs/adr/0007).

db._run is the only faked boundary — no Supabase request is made.
"""
from datetime import datetime, timezone

import pytest

from services import db
from services import db_signals


TICK_TS = datetime(2026, 8, 3, 5, 30, 0, tzinfo=timezone.utc)

MATCH = {
    "warrant_code": "071234",
    "warrant_name": "TSMC Call 01",
    "option_contract": "TSMC202608C1000",
    "underlying_code": "2330",
    "type": "Call",
    "trade": "Buy Warrant / Sell Option",
    "price_diff": 1.25,
    "price_diff_pct": 3.4,
    "strategy": "same_type",
}


class _Query:
    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = []

    def order(self, *a, **kw):
        return self

    def limit(self, n):
        self.filters.append(("limit", n))
        return self

    def lt(self, col, val):
        self.filters.append(("lt", col, val))
        return self

    def execute(self):
        return self.table._execute(self)


class _Table:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def insert(self, rows, **kw):
        return _Query(self, "insert", rows)

    def select(self, cols="*", **kw):
        return _Query(self, "select", cols)

    def delete(self, **kw):
        return _Query(self, "delete")

    def _execute(self, q):
        self.store.ops.append((self.name, q.op, list(q.filters)))
        rows = self.store.rows.setdefault(self.name, [])
        if q.op == "insert":
            rows.extend(q.payload)
            return _Result(list(q.payload))
        if q.op == "delete":
            cutoffs = [f[2] for f in q.filters if f[0] == "lt"]
            if cutoffs:
                gone = [r for r in rows if r["detected_at"] < cutoffs[0]]
                rows[:] = [r for r in rows if r["detected_at"] >= cutoffs[0]]
                return _Result(gone)
            return _Result([])
        return _Result(list(rows))


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
        self.ops = []

    def table(self, name):
        return self.rows.setdefault(name, [])


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_Client(s)))
    return s


def test_to_signal_row_promotes_the_filterable_fields_and_keeps_the_rest():
    row = db_signals.to_signal_row(MATCH, tick_ts=TICK_TS, tick_broker="kgi")

    assert row["strategy"] == "same_type"
    assert row["warrant_code"] == "071234"
    assert row["option_contract"] == "TSMC202608C1000"
    assert row["underlying_code"] == "2330"
    assert row["price_diff"] == 1.25
    assert row["price_diff_pct"] == 3.4
    assert row["tick_broker"] == "kgi"
    assert row["tick_ts"] == TICK_TS.isoformat()
    # The full matcher row survives verbatim, as arb_suggestions does with legs.
    assert row["legs"] == MATCH


def test_to_signal_row_has_no_suggestion_lifecycle_fields():
    row = db_signals.to_signal_row(MATCH, tick_ts=TICK_TS, tick_broker="kgi")
    for field in ("status", "first_seen_at", "last_seen_at", "id"):
        assert field not in row


def test_explicit_strategy_overrides_the_matcher_tag():
    row = db_signals.to_signal_row({**MATCH, "strategy": "same_type"}, strategy="pcp")
    assert row["strategy"] == "pcp"


def test_missing_strategy_falls_back_rather_than_writing_null():
    # strategy is NOT NULL in the schema; a match with no tag must still store.
    row = db_signals.to_signal_row({"price_diff": 1.0})
    assert row["strategy"] == "unknown"


def test_underlying_code_is_stringified_for_a_text_column():
    row = db_signals.to_signal_row({**MATCH, "underlying_code": 2330})
    assert row["underlying_code"] == "2330"


def test_insert_signals_stamps_detected_at(store):
    written = db_signals.insert_signals(
        [db_signals.to_signal_row(MATCH, tick_ts=TICK_TS, tick_broker="kgi")])

    assert written == 1
    rows = store.table("arb_signals")
    assert len(rows) == 1
    assert rows[0]["detected_at"]


def test_the_same_pair_twice_appends_two_rows(store):
    # No dedup at the data layer: two Ticks are two observations (docs/adr/0007).
    row = db_signals.to_signal_row(MATCH, tick_ts=TICK_TS, tick_broker="kgi")
    db_signals.insert_signals([row])
    db_signals.insert_signals([row])

    assert len(store.table("arb_signals")) == 2
    assert all(op[1] == "insert" for op in store.ops)


def test_insert_signals_is_a_noop_on_an_empty_list(store):
    assert db_signals.insert_signals([]) == 0
    assert store.ops == []


def test_delete_signals_before_ages_out_only_older_rows(store):
    store.table("arb_signals").extend([
        {"strategy": "same_type", "detected_at": "2026-08-01T00:00:00+00:00"},
        {"strategy": "same_type", "detected_at": "2026-08-03T00:00:00+00:00"},
    ])

    db_signals.delete_signals_before("2026-08-02T00:00:00+00:00")

    remaining = store.table("arb_signals")
    assert [r["detected_at"] for r in remaining] == ["2026-08-03T00:00:00+00:00"]


def test_list_recent_signals_reads_the_signals_table(store):
    store.table("arb_signals").append(
        {"strategy": "pcp", "detected_at": "2026-08-03T00:00:00+00:00"})

    rows = db_signals.list_recent_signals(limit=10)
    assert [r["strategy"] for r in rows] == ["pcp"]
    assert ("arb_signals", "select", [("limit", 10)]) in store.ops
