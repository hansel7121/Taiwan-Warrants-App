"""The worker->web publication of the pool's actual placement (issue #46).

Real in-process calls; the only faked boundary is db._run / the supabase
client, borrowed from test_watchlist.py so there is one fake Supabase in the
suite. What these pin down is that the table is a mirror of the worker's
current assignment, not a log: publishing replaces it wholesale.
"""
import pytest

from services import db
from services.broker import live_assignment, pool

from tests.services.test_watchlist import _FakeClient, _Store


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    return s


def _slot(broker, user_id, index, *codes):
    return pool.ConnectionSlot(broker=broker, user_id=user_id,
                               connection_index=index, codes=tuple(codes))


def test_a_published_code_reads_back_with_its_connection(store):
    live_assignment.publish(
        pool.Assignment(slots=[_slot("kgi", USER, 0, "030001")]))

    assert live_assignment.read_all() == {"030001": ("kgi", USER)}


def test_every_code_of_every_slot_gets_its_own_row(store):
    """One row per code, not per slot: the reader asks "who carries this code"."""
    live_assignment.publish(pool.Assignment(slots=[
        _slot("kgi", USER, 0, "030001", "030002"),
        _slot("fubon", OTHER, 1, "030003"),
    ]))

    assert live_assignment.read_all() == {
        "030001": ("kgi", USER),
        "030002": ("kgi", USER),
        "030003": ("fubon", OTHER),
    }
    assert len(store.table("live_assignment")) == 3


def test_the_connection_index_is_recorded(store):
    live_assignment.publish(
        pool.Assignment(slots=[_slot("kgi", USER, 2, "030001")]))

    assert store.table("live_assignment")[0]["connection_index"] == 2


def test_publishing_replaces_the_previous_placement(store):
    """A mirror, not a log: a code that moved must not read back on both."""
    live_assignment.publish(
        pool.Assignment(slots=[_slot("kgi", USER, 0, "030001")]))
    live_assignment.publish(
        pool.Assignment(slots=[_slot("fubon", OTHER, 0, "030001")]))

    assert live_assignment.read_all() == {"030001": ("fubon", OTHER)}


def test_publishing_an_empty_assignment_clears_the_table(store):
    """What the worker does when nothing is connected — nothing is carried."""
    live_assignment.publish(
        pool.Assignment(slots=[_slot("kgi", USER, 0, "030001")]))
    live_assignment.publish(pool.Assignment())

    assert live_assignment.read_all() == {}
    assert store.table("live_assignment") == []


def test_reading_a_table_nobody_has_published_to_is_empty(store):
    assert live_assignment.read_all() == {}
