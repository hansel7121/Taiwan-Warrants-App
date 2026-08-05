"""Connection Pool tests.

The assignment algorithm is pure, so it is tested directly with no SDK and no
network. The open/close orchestration is tested against a fake client class
swapped in for the real KGI/Fubon ones, so neither broker SDK needs to be
installed to run this file.
"""
import pytest

from services.broker import pool
from services.broker.base import Tick


IAN = "11111111-1111-1111-1111-111111111111"
HANSEL = "22222222-2222-2222-2222-222222222222"


def _account(user_id, broker, symbols=2, connections=2):
    return pool.BrokerAccount(
        user_id=user_id,
        broker=broker,
        symbols_per_connection=symbols,
        connections=connections,
    )


def _codes(n, prefix="0311"):
    return [f"{prefix}{i:02d}" for i in range(n)]


def test_single_connection_holds_everything_that_fits():
    result = pool.assign([_account(IAN, "kgi", symbols=3)], _codes(3))

    assert result.unassigned == []
    assert len(result.slots) == 1
    slot = result.slots[0]
    assert (slot.broker, slot.user_id, slot.connection_index) == ("kgi", IAN, 0)
    assert slot.codes == tuple(_codes(3))


def test_overflow_opens_a_second_connection_on_the_same_account():
    result = pool.assign([_account(IAN, "kgi", symbols=2, connections=2)], _codes(4))

    assert [s.connection_index for s in result.slots] == [0, 1]
    assert [s.codes for s in result.slots] == [("031100", "031101"), ("031102", "031103")]
    assert result.unassigned == []


def test_unused_connections_produce_no_slots():
    result = pool.assign([_account(IAN, "kgi", symbols=2, connections=5)], _codes(3))

    assert len(result.slots) == 2
    assert result.slots[1].codes == ("031102",)


def test_overflow_moves_to_the_next_account_once_the_first_is_full():
    accounts = [
        _account(IAN, "kgi", symbols=2, connections=1),
        _account(HANSEL, "fubon", symbols=3, connections=1),
    ]

    result = pool.assign(accounts, _codes(5))

    assert [(s.broker, s.user_id) for s in result.slots] == [("kgi", IAN), ("fubon", HANSEL)]
    assert result.slots[1].codes == ("031102", "031103", "031104")


def test_kgi_fills_before_fubon_regardless_of_input_order():
    accounts = [
        _account(HANSEL, "fubon", symbols=2, connections=1),
        _account(IAN, "kgi", symbols=2, connections=1),
    ]

    result = pool.assign(accounts, _codes(4))

    assert [s.broker for s in result.slots] == ["kgi", "fubon"]
    assert result.slots[0].codes == ("031100", "031101")


def test_same_broker_accounts_are_ordered_by_user_id():
    accounts = [
        _account(HANSEL, "kgi", symbols=1, connections=1),
        _account(IAN, "kgi", symbols=1, connections=1),
    ]

    result = pool.assign(accounts, _codes(2))

    assert [s.user_id for s in result.slots] == [IAN, HANSEL]


def test_watchlist_beyond_total_capacity_is_returned_as_unassigned():
    accounts = [
        _account(IAN, "kgi", symbols=2, connections=2),
        _account(HANSEL, "fubon", symbols=2, connections=1),
    ]

    result = pool.assign(accounts, _codes(10))

    assert sum(len(s.codes) for s in result.slots) == 6
    assert result.unassigned == _codes(10)[6:]


def test_empty_watchlist_assigns_nothing():
    result = pool.assign([_account(IAN, "kgi")], [])

    assert result.slots == []
    assert result.unassigned == []


def test_no_accounts_leaves_the_whole_watchlist_unassigned():
    result = pool.assign([], _codes(3))

    assert result.slots == []
    assert result.unassigned == _codes(3)


def test_capacity_totals_every_account():
    accounts = [
        _account(IAN, "kgi", symbols=30, connections=2),
        _account(HANSEL, "fubon", symbols=200, connections=5),
    ]

    assert pool.capacity(accounts) == 1060


# ── open / close orchestration ───────────────────────────────────────────────


class _FakeClient:
    instances = []

    def __init__(self, credential):
        self.credential = credential
        self.subscribed = []
        self.on_tick = None
        self.logged_in = False
        self.logged_out = False
        _FakeClient.instances.append(self)

    @classmethod
    def from_stored(cls, user_id):
        return cls({"user_id": user_id})

    def login(self):
        self.logged_in = True

    def logout(self):
        self.logged_out = True

    def subscribe(self, codes, on_tick):
        self.subscribed = list(codes)
        self.on_tick = on_tick


@pytest.fixture
def fake_clients(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(pool, "client_class", lambda broker: _FakeClient)
    return _FakeClient.instances


def test_open_logs_in_and_subscribes_one_client_per_slot(fake_clients):
    accounts = [
        _account(IAN, "kgi", symbols=2, connections=2),
        _account(HANSEL, "fubon", symbols=2, connections=1),
    ]
    p = pool.ConnectionPool(accounts, _codes(5), on_tick=lambda tick: None)

    p.open()

    assert len(fake_clients) == 3
    assert all(c.logged_in for c in fake_clients)
    assert [c.subscribed for c in fake_clients] == [
        ["031100", "031101"],
        ["031102", "031103"],
        ["031104"],
    ]
    assert [c.credential["user_id"] for c in fake_clients] == [IAN, IAN, HANSEL]


def test_every_client_feeds_the_one_pool_callback(fake_clients):
    seen = []
    p = pool.ConnectionPool(
        [_account(IAN, "kgi", symbols=1, connections=2)],
        _codes(2),
        on_tick=seen.append,
    )
    p.open()

    for client in fake_clients:
        client.on_tick(Tick(code=client.subscribed[0], price=1.0, ts=None, broker="kgi"))

    assert [t.code for t in seen] == ["031100", "031101"]


def test_close_logs_out_every_open_client(fake_clients):
    p = pool.ConnectionPool(
        [_account(IAN, "kgi", symbols=1, connections=2)], _codes(2), on_tick=lambda t: None
    )
    p.open()

    p.close()

    assert all(c.logged_out for c in fake_clients)
    assert p.clients == []


def test_login_failure_propagates(fake_clients, monkeypatch):
    def _boom(self):
        raise RuntimeError("broker down")

    monkeypatch.setattr(_FakeClient, "login", _boom)
    p = pool.ConnectionPool([_account(IAN, "kgi")], _codes(1), on_tick=lambda t: None)

    with pytest.raises(RuntimeError):
        p.open()


# ── incremental reassignment ─────────────────────────────────────────────────


def _current(accounts, codes):
    """The assignment a pool would be holding after a from-scratch login."""
    return pool.assign(accounts, codes)


def test_added_code_subscribes_on_the_connection_that_has_room():
    accounts = [_account(IAN, "kgi", symbols=3, connections=1)]
    current = _current(accounts, _codes(2))

    diff = pool.reassign(accounts, current, _codes(3))

    assert diff.unsubscribes == []
    assert diff.rejected == []
    assert len(diff.subscribes) == 1
    op = diff.subscribes[0]
    assert (op.broker, op.user_id, op.connection_index) == ("kgi", IAN, 0)
    assert op.codes == ("031102",)


def test_added_code_leaves_the_other_connections_untouched():
    accounts = [_account(IAN, "kgi", symbols=2, connections=3)]
    current = _current(accounts, _codes(2))

    diff = pool.reassign(accounts, current, _codes(3))

    assert [op.connection_index for op in diff.subscribes] == [1]
    assert diff.assignment.slots[0].codes == ("031100", "031101")


def test_removed_code_unsubscribes_only_that_code():
    accounts = [_account(IAN, "kgi", symbols=3, connections=1)]
    current = _current(accounts, _codes(3))

    diff = pool.reassign(accounts, current, ["031100", "031102"])

    assert diff.subscribes == []
    assert len(diff.unsubscribes) == 1
    op = diff.unsubscribes[0]
    assert (op.broker, op.user_id, op.connection_index) == ("kgi", IAN, 0)
    assert op.codes == ("031101",)
    assert diff.assignment.slots[0].codes == ("031100", "031102")


def test_removing_frees_capacity_for_an_add_in_the_same_diff():
    accounts = [_account(IAN, "kgi", symbols=2, connections=1)]
    current = _current(accounts, _codes(2))

    diff = pool.reassign(accounts, current, ["031100", "031199"])

    assert diff.rejected == []
    assert [op.codes for op in diff.unsubscribes] == [("031101",)]
    assert [op.codes for op in diff.subscribes] == [("031199",)]
    assert diff.assignment.slots[0].codes == ("031100", "031199")


def test_add_past_total_capacity_is_rejected():
    accounts = [_account(IAN, "kgi", symbols=2, connections=1)]
    current = _current(accounts, _codes(2))

    diff = pool.reassign(accounts, current, _codes(2) + ["031199"])

    assert diff.rejected == ["031199"]
    assert diff.subscribes == []
    assert diff.unsubscribes == []
    assert diff.assignment.slots[0].codes == ("031100", "031101")
    assert diff.assignment.unassigned == []


def test_rejected_adds_never_evict_a_watched_code():
    accounts = [_account(IAN, "kgi", symbols=2, connections=2)]
    current = _current(accounts, _codes(4))

    diff = pool.reassign(accounts, current, _codes(4) + ["031198", "031199"])

    assert diff.rejected == ["031198", "031199"]
    assert [s.codes for s in diff.assignment.slots] == [s.codes for s in current.slots]


def test_unchanged_watchlist_produces_no_ops():
    accounts = [
        _account(IAN, "kgi", symbols=2, connections=2),
        _account(HANSEL, "fubon", symbols=2, connections=1),
    ]
    current = _current(accounts, _codes(5))

    diff = pool.reassign(accounts, current, _codes(5))

    assert diff.subscribes == []
    assert diff.unsubscribes == []
    assert diff.rejected == []


def test_untouched_codes_produce_no_ops_across_an_add_and_a_remove():
    accounts = [_account(IAN, "kgi", symbols=2, connections=3)]
    current = _current(accounts, _codes(5))

    diff = pool.reassign(accounts, current, ["031100", "031101", "031103", "031104", "031199"])

    # Only connection 1 (which held 031102) sees any traffic.
    assert [op.connection_index for op in diff.unsubscribes] == [1]
    assert [op.connection_index for op in diff.subscribes] == [1]
    assert diff.assignment.slots[0].codes == ("031100", "031101")
    assert diff.assignment.slots[2].codes == ("031104",)


def test_reassign_is_deterministic():
    accounts = [
        _account(HANSEL, "fubon", symbols=2, connections=2),
        _account(IAN, "kgi", symbols=2, connections=2),
    ]
    current = _current(accounts, _codes(6))
    watchlist = ["031100", "031102", "031104", "031105", "031196", "031197"]

    first = pool.reassign(accounts, current, watchlist)
    second = pool.reassign(accounts, current, watchlist)

    assert first == second


def test_code_that_was_over_capacity_is_picked_up_once_room_appears():
    accounts = [_account(IAN, "kgi", symbols=2, connections=1)]
    current = _current(accounts, _codes(3))

    assert current.unassigned == ["031102"]

    diff = pool.reassign(accounts, current, ["031100", "031102"])

    assert diff.rejected == []
    assert [op.codes for op in diff.unsubscribes] == [("031101",)]
    assert [op.codes for op in diff.subscribes] == [("031102",)]


def test_reassign_from_nothing_matches_a_from_scratch_assign():
    accounts = [
        _account(IAN, "kgi", symbols=2, connections=2),
        _account(HANSEL, "fubon", symbols=2, connections=1),
    ]

    diff = pool.reassign(accounts, pool.Assignment(), _codes(5))

    assert diff.unsubscribes == []
    assert diff.assignment.slots == pool.assign(accounts, _codes(5)).slots
    assert [op.codes for op in diff.subscribes] == [s.codes for s in diff.assignment.slots]


def test_codes_on_a_vanished_account_move_to_a_surviving_one():
    before = [
        _account(IAN, "kgi", symbols=2, connections=1),
        _account(HANSEL, "fubon", symbols=2, connections=1),
    ]
    current = _current(before, _codes(4))
    after = [_account(IAN, "kgi", symbols=4, connections=1)]

    diff = pool.reassign(after, current, _codes(4))

    assert [(op.broker, op.codes) for op in diff.unsubscribes] == [("fubon", ("031102", "031103"))]
    assert [(op.broker, op.codes) for op in diff.subscribes] == [("kgi", ("031102", "031103"))]
    assert diff.assignment.slots[0].codes == tuple(_codes(4))


def test_empty_watchlist_unsubscribes_everything():
    accounts = [_account(IAN, "kgi", symbols=2, connections=2)]
    current = _current(accounts, _codes(3))

    diff = pool.reassign(accounts, current, [])

    assert [op.codes for op in diff.unsubscribes] == [("031100", "031101"), ("031102",)]
    assert diff.subscribes == []
    assert diff.assignment.slots == []
