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
