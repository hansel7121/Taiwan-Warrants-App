"""Worker reconciliation: desired state in, broker logins, subscriptions out.

Nothing real is touched — no broker, no network, no Supabase. The faked
boundaries are the broker client classes (stand-ins that record login/logout)
and the two desired_state calls the worker makes, which is the whole surface the
worker has in this ticket: it logs in, it logs out, it reports what happened.

The Watchlist half (#44) fakes the same boundaries one level up: the fake
clients below record subscribe/unsubscribe, `pool.accounts_for` stands in for
the stored Capacity Tiers, and the market-hours gate is forced, so the tests
say what the worker CALLS on which connection rather than what a broker does.

The tick half (#46) fakes one boundary lower still: db._run, borrowed from
test_watchlist.py so there is one fake Supabase client in the suite rather than
two. A tick handed to the callback the worker actually registers must come out
as a live_prices row.

The real SDKs are deliberately not importable here (kgisuperpy / fubon_neo ship
only in the worker image), which is exactly why broker_worker imports the client
modules inside client_class() rather than at module scope. One test below pins
that down by injecting stub SDK modules and checking the dispatch resolves.
"""
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import broker_worker
from services import db
from services.broker import live_assignment, pool
from services.broker.base import BrokerConnectionError, Depth, Tick
from tests.services.test_watchlist import _FakeClient, _Store


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


class _FakeBrokerClient:
    """Stand-in for a BrokerClient: records the calls, optionally fails."""

    def __init__(self, user_id, broker, login_error=None, logout_error=None):
        self.user_id = user_id
        self.broker = broker
        self.login_error = login_error
        self.logout_error = logout_error
        self.logged_in = False
        self.logged_out = False
        self.subscribe_error = None
        self.unsubscribe_error = None
        self.subscribes = []      # each subscribe() call's codes, in order
        self.unsubscribes = []    # each unsubscribe() call's codes, in order
        self.on_tick = None
        self.on_depth = None

    def subscribe(self, codes, on_tick, on_depth=None):
        self.subscribes.append(list(codes))
        self.on_tick = on_tick
        self.on_depth = on_depth
        if self.subscribe_error:
            raise self.subscribe_error

    def unsubscribe(self, codes):
        self.unsubscribes.append(list(codes))
        if self.unsubscribe_error:
            raise self.unsubscribe_error

    def login(self):
        if self.login_error:
            raise self.login_error
        self.logged_in = True

    def logout(self):
        self.logged_out = True
        if self.logout_error:
            raise self.logout_error


class _Broker:
    """Fake client class: `from_stored` hands back a prepared instance."""

    def __init__(self, broker, errors=None):
        self.broker = broker
        self.errors = errors or {}
        self.built = []

    def from_stored(self, user_id):
        client = _FakeBrokerClient(user_id, self.broker,
                                   login_error=self.errors.get(user_id))
        self.built.append(client)
        return client


@pytest.fixture
def worker(monkeypatch):
    """A Worker whose status writes are captured instead of sent to Supabase."""
    w = broker_worker.Worker()
    w.statuses = []
    monkeypatch.setattr(
        broker_worker.desired_state, "set_worker_status",
        lambda user_id, broker, status: w.statuses.append((user_id, broker, status)))
    return w


def _brokers(worker, monkeypatch, **classes):
    """Point the worker's dispatch at fake client classes, keyed by broker."""
    monkeypatch.setattr(broker_worker, "client_class", lambda b: classes[b])
    return classes


def _row(user_id, broker, state):
    return {"user_id": user_id, "broker": broker, "desired_state": state}


def _account(user_id, broker, symbols_per_connection=3, connections=1):
    return pool.BrokerAccount(user_id=user_id, broker=broker,
                              symbols_per_connection=symbols_per_connection,
                              connections=connections)


def _live(worker, monkeypatch, accounts, open_accounts=None):
    """Open a fake session per account, and let the pool see the same accounts.

    `open_accounts` defaults to all of them; pass a subset to model an account
    whose credential is stored but whose session is not up.
    """
    monkeypatch.setattr(broker_worker, "is_market_open", lambda market: True)
    monkeypatch.setattr(
        broker_worker.pool, "accounts_for",
        lambda user_ids: [a for a in accounts if a.user_id in set(user_ids)])
    opened = accounts if open_accounts is None else open_accounts
    clients = {(a.user_id, a.broker): _FakeBrokerClient(a.user_id, a.broker)
               for a in opened}
    worker._clients.update(clients)
    return clients


# ── connecting ───────────────────────────────────────────────────────────

def test_a_connect_row_logs_in_and_reports_connected(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]

    worker.reconcile([_row(USER, "kgi", "connect")])

    assert [c.logged_in for c in kgi.built] == [True]
    assert worker.statuses == [(USER, "kgi", "connected")]


def test_an_account_already_connected_is_not_logged_in_again(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]
    rows = [_row(USER, "kgi", "connect")]

    worker.reconcile(rows)
    worker.reconcile(rows)
    worker.reconcile(rows)

    assert len(kgi.built) == 1
    assert worker.statuses == [(USER, "kgi", "connected")]


def test_each_broker_account_gets_its_own_client(worker, monkeypatch):
    classes = _brokers(worker, monkeypatch,
                       kgi=_Broker("kgi"), fubon=_Broker("fubon"))

    worker.reconcile([_row(USER, "kgi", "connect"),
                      _row(USER, "fubon", "connect"),
                      _row(OTHER, "kgi", "connect")])

    assert [c.user_id for c in classes["kgi"].built] == [USER, OTHER]
    assert [c.user_id for c in classes["fubon"].built] == [USER]
    assert sorted(worker.statuses) == sorted([
        (USER, "kgi", "connected"), (USER, "fubon", "connected"),
        (OTHER, "kgi", "connected")])


def test_a_row_the_worker_does_not_recognise_is_ignored(worker, monkeypatch):
    _brokers(worker, monkeypatch, kgi=_Broker("kgi"))

    worker.reconcile([_row(USER, "etrade", "connect"),
                      _row(USER, "kgi", "disconnect")])

    assert worker.statuses == []


# ── failure isolation ────────────────────────────────────────────────────

def test_a_rejected_login_reports_disconnected_without_raising(worker, monkeypatch):
    _brokers(worker, monkeypatch,
             kgi=_Broker("kgi", errors={USER: BrokerConnectionError("bad password")}))

    worker.reconcile([_row(USER, "kgi", "connect")])

    assert worker.statuses == [(USER, "kgi", "disconnected")]


def test_an_unexpected_login_error_is_also_contained(worker, monkeypatch):
    """The SDKs fail in undocumented ways; none of them may reach the poll loop."""
    _brokers(worker, monkeypatch,
             kgi=_Broker("kgi", errors={USER: RuntimeError("native crash")}))

    worker.reconcile([_row(USER, "kgi", "connect")])

    assert worker.statuses == [(USER, "kgi", "disconnected")]


def test_a_failed_account_is_retried_on_the_next_poll(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch,
                   kgi=_Broker("kgi", errors={USER: BrokerConnectionError("nope")})
                   )["kgi"]
    rows = [_row(USER, "kgi", "connect")]

    worker.reconcile(rows)
    kgi.errors = {}
    worker.reconcile(rows)

    assert worker.statuses == [(USER, "kgi", "disconnected"), (USER, "kgi", "connected")]


def test_one_accounts_failure_does_not_block_another(worker, monkeypatch):
    """Two users share one worker; neither may take the other offline."""
    classes = _brokers(
        worker, monkeypatch,
        kgi=_Broker("kgi", errors={USER: BrokerConnectionError("bad credential")}),
        fubon=_Broker("fubon"))

    worker.reconcile([_row(USER, "kgi", "connect"),
                      _row(OTHER, "kgi", "connect"),
                      _row(USER, "fubon", "connect")])

    assert [(c.user_id, c.logged_in) for c in classes["kgi"].built] == [
        (USER, False), (OTHER, True)]
    assert classes["fubon"].built[0].logged_in is True
    assert sorted(worker.statuses) == sorted([
        (USER, "kgi", "disconnected"), (OTHER, "kgi", "connected"),
        (USER, "fubon", "connected")])


def test_a_failed_status_write_does_not_lose_the_session(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]

    def boom(*_a):
        raise ConnectionError("supabase unreachable")

    monkeypatch.setattr(broker_worker.desired_state, "set_worker_status", boom)
    worker.reconcile([_row(USER, "kgi", "connect")])

    # Still connected and still tracked: a stale UI beats a dropped session.
    assert kgi.built[0].logged_in is True
    monkeypatch.setattr(
        broker_worker.desired_state, "set_worker_status",
        lambda user_id, broker, status: worker.statuses.append((user_id, broker, status)))
    worker.reconcile([_row(USER, "kgi", "connect")])
    assert len(kgi.built) == 1


# ── disconnecting ────────────────────────────────────────────────────────

def test_a_disconnect_row_logs_out_and_reports_stopped(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]
    worker.reconcile([_row(USER, "kgi", "connect")])
    worker.statuses.clear()

    worker.reconcile([_row(USER, "kgi", "disconnect")])

    assert kgi.built[0].logged_out is True
    assert worker.statuses == [(USER, "kgi", "stopped")]


def test_a_row_that_disappears_is_treated_as_a_disconnect(worker, monkeypatch):
    """A deleted row and an explicit 'disconnect' say the same thing."""
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]
    worker.reconcile([_row(USER, "kgi", "connect")])
    worker.statuses.clear()

    worker.reconcile([])

    assert kgi.built[0].logged_out is True
    assert worker.statuses == [(USER, "kgi", "stopped")]


def test_disconnecting_one_account_leaves_the_other_connected(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]
    worker.reconcile([_row(USER, "kgi", "connect"), _row(OTHER, "kgi", "connect")])
    worker.statuses.clear()

    worker.reconcile([_row(OTHER, "kgi", "connect")])

    by_user = {c.user_id: c for c in kgi.built}
    assert by_user[USER].logged_out is True
    assert by_user[OTHER].logged_out is False
    assert worker.statuses == [(USER, "kgi", "stopped")]


def test_a_failing_logout_still_frees_the_account(worker, monkeypatch):
    """A broker erroring on logout must not strand the row as 'connected'."""
    _brokers(worker, monkeypatch, kgi=_Broker("kgi"))
    worker.reconcile([_row(USER, "kgi", "connect")])
    worker._clients[(USER, "kgi")].logout_error = RuntimeError("socket already gone")
    worker.statuses.clear()

    worker.reconcile([])

    assert worker.statuses == [(USER, "kgi", "stopped")]
    assert worker._clients == {}


def test_reconnecting_after_a_disconnect_builds_a_fresh_client(worker, monkeypatch):
    kgi = _brokers(worker, monkeypatch, kgi=_Broker("kgi"))["kgi"]

    worker.reconcile([_row(USER, "kgi", "connect")])
    worker.reconcile([_row(USER, "kgi", "disconnect")])
    worker.reconcile([_row(USER, "kgi", "connect")])

    assert len(kgi.built) == 2
    assert worker.statuses == [(USER, "kgi", "connected"), (USER, "kgi", "stopped"),
                               (USER, "kgi", "connected")]


def test_shutdown_logs_every_session_out(worker, monkeypatch):
    classes = _brokers(worker, monkeypatch,
                       kgi=_Broker("kgi"), fubon=_Broker("fubon"))
    worker.reconcile([_row(USER, "kgi", "connect"), _row(OTHER, "fubon", "connect")])
    worker.statuses.clear()

    worker.shutdown()

    assert classes["kgi"].built[0].logged_out is True
    assert classes["fubon"].built[0].logged_out is True
    assert sorted(worker.statuses) == sorted([
        (USER, "kgi", "stopped"), (OTHER, "fubon", "stopped")])
    assert worker._clients == {}


# ── plumbing ─────────────────────────────────────────────────────────────

def test_the_poll_reads_every_users_desired_state(worker, monkeypatch):
    _brokers(worker, monkeypatch, kgi=_Broker("kgi"))
    monkeypatch.setattr(broker_worker.desired_state, "list_all_desired_states",
                        lambda: [_row(OTHER, "kgi", "connect")])

    worker.poll_desired_state()

    assert worker.statuses == [(OTHER, "kgi", "connected")]


def test_heartbeat_counts_the_live_sessions(worker, monkeypatch, caplog):
    _brokers(worker, monkeypatch, kgi=_Broker("kgi"))
    worker.reconcile([_row(USER, "kgi", "connect")])

    with caplog.at_level("INFO", logger="broker_worker"):
        worker.heartbeat()

    assert "1 account(s) connected" in caplog.text


def test_a_job_that_raises_never_stops_the_scheduler():
    def explode():
        raise RuntimeError("boom")

    broker_worker._job("desired_state", explode)()   # must not raise


def test_the_scheduler_runs_the_poll_and_the_heartbeat(worker):
    # Built, never started: registration is the thing worth pinning, and a live
    # scheduler in a test would start polling Supabase.
    sched = broker_worker.build_scheduler(worker)

    intervals = sorted(j.trigger.interval.total_seconds() for j in sched.get_jobs())
    assert intervals == sorted([broker_worker.DESIRED_POLL_SEC,
                                broker_worker.WATCHLIST_POLL_SEC,
                                broker_worker.HEARTBEAT_SEC,
                                broker_worker.TICK_CLEANUP_SEC])


def test_client_class_dispatches_to_the_real_clients_lazily():
    """The SDK imports must stay inside client_class, or nothing else imports.

    Stub SDK modules stand in for kgisuperpy / fubon_neo, which exist only in the
    worker image — if these imports ever move to module scope, this file (and
    every other test importing broker_worker) stops collecting on a dev machine.
    """
    fubon_sdk = types.ModuleType("fubon_neo.sdk")
    fubon_sdk.FubonSDK = object
    fubon = types.ModuleType("fubon_neo")
    fubon.sdk = fubon_sdk
    stubs = {"kgisuperpy": types.ModuleType("kgisuperpy"),
             "fubon_neo": fubon, "fubon_neo.sdk": fubon_sdk}

    with patch.dict(sys.modules, stubs):
        assert broker_worker.client_class("kgi").broker == "kgi"
        assert broker_worker.client_class("fubon").broker == "fubon"

    with pytest.raises(ValueError):
        broker_worker.client_class("etrade")


# ── the watchlist poll (#44) ─────────────────────────────────────────────

def test_a_new_code_is_subscribed_on_the_open_connection(worker, monkeypatch):
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234"])

    assert kgi.subscribes == [["031234"]]
    assert kgi.unsubscribes == []


def test_ticks_are_pointed_at_the_relay(worker, monkeypatch):
    """What a subscribe hands the broker is the thing that writes live_prices."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234"])

    assert kgi.on_tick is broker_worker._relay_tick


def test_depth_is_also_pointed_at_the_relay(worker, monkeypatch):
    """Issue #51: the same subscribe call wires the depth relay alongside ticks."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234"])

    assert kgi.on_depth is broker_worker._relay_depth


def test_an_edit_leaves_the_codes_that_did_not_change_alone(worker, monkeypatch):
    """The whole point of diffing: a live subscription nobody touched is not
    dropped and re-taken just because the list around it moved."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234"])
    worker.apply_watchlist(["031234", "035678"])

    assert kgi.subscribes == [["031234"], ["035678"]]
    assert kgi.unsubscribes == []


def test_a_removed_code_is_unsubscribed(worker, monkeypatch):
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234", "035678"])
    worker.apply_watchlist(["031234"])

    assert kgi.unsubscribes == [["035678"]]


def test_an_unchanged_watchlist_costs_nothing(worker, monkeypatch):
    """This runs every few seconds; a steady list must be a no-op."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]

    worker.apply_watchlist(["031234"])
    worker.apply_watchlist(["031234"])
    worker.apply_watchlist(["031234"])

    assert kgi.subscribes == [["031234"]]
    assert kgi.unsubscribes == []


def test_a_second_connection_is_only_touched_once_the_first_is_full(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=2),
                     _account(OTHER, "fubon", symbols_per_connection=2)])

    worker.apply_watchlist(["031111", "032222"])

    assert clients[(USER, "kgi")].subscribes == [["031111", "032222"]]
    assert clients[(OTHER, "fubon")].subscribes == []


def test_a_code_that_overflows_the_first_connection_lands_on_the_next(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=2),
                     _account(OTHER, "fubon", symbols_per_connection=2)])

    worker.apply_watchlist(["031111", "032222", "033333"])

    assert clients[(USER, "kgi")].subscribes == [["031111", "032222"]]
    assert clients[(OTHER, "fubon")].subscribes == [["033333"]]


# ── which accounts the poll may plan against ─────────────────────────────

def test_an_account_without_an_open_session_is_never_given_codes(worker, monkeypatch):
    """Its Capacity Tier is real but unreachable: nothing here can subscribe on
    a connection the worker is not holding a client for."""
    accounts = [_account(USER, "kgi", symbols_per_connection=1),
                _account(OTHER, "fubon", symbols_per_connection=5)]
    clients = _live(worker, monkeypatch, accounts, open_accounts=accounts[:1])

    worker.apply_watchlist(["031111", "032222"])

    assert clients[(USER, "kgi")].subscribes == [["031111"]]
    assert (OTHER, "fubon") not in clients
    # 032222 did not fit the one reachable connection, and says so.
    assert worker._rejected == {"032222"}


def test_a_reconnected_account_picks_its_codes_up_on_the_next_poll(worker, monkeypatch):
    accounts = [_account(USER, "kgi", symbols_per_connection=1),
                _account(OTHER, "fubon", symbols_per_connection=5)]
    _live(worker, monkeypatch, accounts, open_accounts=accounts[:1])
    worker.apply_watchlist(["031111", "032222"])

    fubon = _FakeBrokerClient(OTHER, "fubon")
    worker._clients[(OTHER, "fubon")] = fubon
    worker.apply_watchlist(["031111", "032222"])

    assert fubon.subscribes == [["032222"]]
    assert worker._rejected == set()


def test_only_the_connections_the_worker_holds_count_as_capacity(worker, monkeypatch):
    """A tier of 4 connections is 4 only if 4 are open; the worker opens one."""
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=1, connections=4)])

    worker.apply_watchlist(["031111", "032222"])

    assert clients[(USER, "kgi")].subscribes == [["031111"]]
    assert worker._rejected == {"032222"}


def test_overflow_is_warned_about_rather_than_raised(worker, monkeypatch, caplog):
    _live(worker, monkeypatch, [_account(USER, "kgi", symbols_per_connection=1)])

    with caplog.at_level("WARNING", logger="broker_worker"):
        worker.apply_watchlist(["031111", "032222"])

    assert "032222" in caplog.text


def test_an_unchanged_overflow_is_not_re_logged(worker, monkeypatch, caplog):
    _live(worker, monkeypatch, [_account(USER, "kgi", symbols_per_connection=1)])
    worker.apply_watchlist(["031111", "032222"])
    caplog.clear()

    with caplog.at_level("WARNING", logger="broker_worker"):
        worker.apply_watchlist(["031111", "032222"])

    assert "032222" not in caplog.text


def test_losing_every_session_forgets_what_was_subscribed(worker, monkeypatch):
    """The subscriptions died with the sessions, so the next poll must re-take
    them rather than try to unsubscribe connections that are gone."""
    _live(worker, monkeypatch, [_account(USER, "kgi")])
    worker.apply_watchlist(["031234"])

    worker._clients.clear()
    worker.apply_watchlist(["031234"])
    assert worker._current.slots == []

    kgi = _FakeBrokerClient(USER, "kgi")
    worker._clients[(USER, "kgi")] = kgi
    worker.apply_watchlist(["031234"])

    assert kgi.subscribes == [["031234"]]


# ── market hours ─────────────────────────────────────────────────────────

def test_the_poll_does_nothing_while_the_market_is_shut(worker, monkeypatch):
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]
    monkeypatch.setattr(broker_worker, "is_market_open", lambda market: False)
    monkeypatch.setattr(broker_worker.watchlist, "list_codes",
                        lambda: pytest.fail("read the watchlist out of hours"))

    worker.poll_watchlist()

    assert kgi.subscribes == []


def test_the_poll_reads_the_shared_watchlist_during_market_hours(worker, monkeypatch):
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]
    monkeypatch.setattr(broker_worker.watchlist, "list_codes", lambda: ["031234"])

    worker.poll_watchlist()

    assert kgi.subscribes == [["031234"]]


def test_the_poll_is_gated_on_the_warrant_market(worker, monkeypatch):
    """tw_equity, like the warrant sync grid: a warrant trades on TWSE hours."""
    asked = []
    monkeypatch.setattr(broker_worker, "is_market_open",
                        lambda market: asked.append(market) or False)

    worker.poll_watchlist()

    assert asked == ["tw_equity"]


# ── failure isolation ────────────────────────────────────────────────────

def test_one_connections_failed_subscribe_does_not_block_another(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=1),
                     _account(OTHER, "fubon", symbols_per_connection=1)])
    clients[(USER, "kgi")].subscribe_error = RuntimeError("websocket closed")

    worker.apply_watchlist(["031111", "032222"])   # must not raise

    assert clients[(OTHER, "fubon")].subscribes == [["032222"]]


def test_a_refused_subscribe_is_retried_on_the_next_poll(worker, monkeypatch):
    """Same contract as a failed login: the code comes back round again."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]
    kgi.subscribe_error = RuntimeError("websocket closed")
    worker.apply_watchlist(["031234"])

    kgi.subscribe_error = None
    worker.apply_watchlist(["031234"])

    assert kgi.subscribes == [["031234"], ["031234"]]


def test_a_code_that_did_subscribe_is_not_retried_alongside_one_that_failed(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=1),
                     _account(OTHER, "fubon", symbols_per_connection=1)])
    clients[(USER, "kgi")].subscribe_error = RuntimeError("websocket closed")
    worker.apply_watchlist(["031111", "032222"])

    worker.apply_watchlist(["031111", "032222"])

    assert clients[(OTHER, "fubon")].subscribes == [["032222"]]


def test_one_connections_failed_unsubscribe_does_not_block_another(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=1),
                     _account(OTHER, "fubon", symbols_per_connection=1)])
    worker.apply_watchlist(["031111", "032222"])
    clients[(USER, "kgi")].unsubscribe_error = RuntimeError("already gone")

    worker.apply_watchlist([])   # must not raise

    assert clients[(OTHER, "fubon")].unsubscribes == [["032222"]]


def test_a_session_closed_mid_diff_is_skipped_not_crashed_on(worker, monkeypatch):
    clients = _live(worker, monkeypatch,
                    [_account(USER, "kgi", symbols_per_connection=1),
                     _account(OTHER, "fubon", symbols_per_connection=1)])
    worker.apply_watchlist(["031111", "032222"])

    # The desired-state loop drops one session; the pool still plans for it
    # because accounts_for is faked, which is the race this guards.
    worker._clients.pop((USER, "kgi"))
    worker.apply_watchlist([])

    assert clients[(OTHER, "fubon")].unsubscribes == [["032222"]]


def test_the_published_placement_mirrors_what_the_worker_holds(worker, monkeypatch):
    """app.py reads this table instead of recomputing a placement (docs/adr/0006),
    so it has to say exactly what self._current says."""
    _live(worker, monkeypatch,
          [_account(USER, "kgi", symbols_per_connection=1),
           _account(OTHER, "fubon", symbols_per_connection=1)])

    worker.apply_watchlist(["031111", "032222"])

    assert live_assignment.read_all() == {
        "031111": ("kgi", USER),
        "032222": ("fubon", OTHER),
    }


def test_a_sticky_code_is_published_on_the_connection_it_stayed_on(worker, monkeypatch):
    """reassign keeps 032222 on fubon; a fresh assign would move it to kgi."""
    _live(worker, monkeypatch,
          [_account(USER, "kgi", symbols_per_connection=1),
           _account(OTHER, "fubon", symbols_per_connection=1)])

    worker.apply_watchlist(["031111", "032222"])
    worker.apply_watchlist(["032222"])

    assert live_assignment.read_all() == {"032222": ("fubon", OTHER)}


def test_nothing_connected_publishes_an_empty_placement(worker, monkeypatch):
    """The reset path: no session means no connection carries anything."""
    _live(worker, monkeypatch, [_account(USER, "kgi")])
    worker.apply_watchlist(["031234"])
    worker._clients.clear()

    worker.apply_watchlist(["031234"])

    assert live_assignment.read_all() == {}


def test_a_failed_publish_does_not_break_the_watchlist_loop(worker, monkeypatch):
    """Same reasoning as set_status: Supabase being down must not drop sessions."""
    _live(worker, monkeypatch, [_account(USER, "kgi")])
    monkeypatch.setattr(live_assignment, "publish", lambda a: (_ for _ in ()).throw(
        ConnectionError("supabase unreachable")))

    worker.apply_watchlist(["031234"])

    assert worker._current.slots


# ── the live-price relay (#46) ───────────────────────────────────────────

@pytest.fixture(autouse=True)
def store(monkeypatch):
    """A fake Supabase, so a tick's write lands somewhere a test can read.

    autouse: apply_watchlist also publishes the pool's placement, so every
    watchlist test would otherwise reach for a real Supabase client.
    """
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    return s


def _tick(code="031234", price=1.23, broker="kgi", qty=None):
    return Tick(code=code, price=price, broker=broker, qty=qty,
                ts=datetime(2026, 8, 4, 5, 30, 0, tzinfo=timezone.utc))


def test_a_tick_is_written_to_the_live_price_relay(worker, monkeypatch, store):
    """The whole point: a print in the worker becomes a row the web app can read."""
    kgi = _live(worker, monkeypatch, [_account(USER, "kgi")])[(USER, "kgi")]
    worker.apply_watchlist(["031234"])

    kgi.on_tick(_tick())

    assert store.table("live_prices") == [{
        "code": "031234",
        "price": 1.23,
        "ts": "2026-08-04T05:30:00+00:00",
        "broker": "kgi",
        "qty": None,
    }]


def test_tick_qty_is_relayed(store):
    """kgi_client populates qty from tick.volume (issue #54); the relay must
    not drop it on the way to the row the web app reads."""
    broker_worker._relay_tick(_tick(qty=5000))

    assert store.table("live_prices")[0]["qty"] == 5000


def test_the_relay_keeps_one_row_per_code(store):
    """It is a cache, not a tick history: the next print overwrites the last.

    Also pins the conflict key: the fake falls back to (user_id, broker) when a
    caller passes no on_conflict, and live_prices has neither column.
    """
    broker_worker._relay_tick(_tick(price=1.23))
    broker_worker._relay_tick(_tick(price=1.45))

    rows = store.table("live_prices")
    assert len(rows) == 1
    assert rows[0]["price"] == 1.45


def test_two_codes_get_two_rows(store):
    broker_worker._relay_tick(_tick(code="031111"))
    broker_worker._relay_tick(_tick(code="032222"))

    assert sorted(r["code"] for r in store.table("live_prices")) == ["031111", "032222"]


def test_the_writing_broker_is_recorded(store):
    """Which connection produced the print, straight off the Tick."""
    broker_worker._relay_tick(_tick(broker="fubon"))

    assert store.table("live_prices")[0]["broker"] == "fubon"


def test_a_failed_write_is_not_swallowed_here(store, monkeypatch):
    """Thin wrapper, per desired_state: the caller decides, and the SDK callback
    thread logging a traceback beats prices silently freezing."""
    monkeypatch.setattr(db, "_run", lambda build: (_ for _ in ()).throw(
        ConnectionError("supabase unreachable")))

    with pytest.raises(ConnectionError):
        broker_worker._relay_tick(_tick())


# ── the tick history relay (#52) ──────────────────────────────────────────

def test_a_tick_is_also_appended_to_the_tick_history(store):
    broker_worker._relay_tick(_tick())

    assert store.table("live_price_ticks") == [{
        "code": "031234",
        "broker": "kgi",
        "price": 1.23,
        "qty": None,
        "ts": "2026-08-04T05:30:00+00:00",
    }]


def test_repeated_ticks_accumulate_in_the_history_unlike_the_cache(store):
    """The whole point of a second table: live_prices overwrites, this appends."""
    broker_worker._relay_tick(_tick(price=1.23))
    broker_worker._relay_tick(_tick(price=1.45))

    assert [r["price"] for r in store.table("live_price_ticks")] == [1.23, 1.45]
    assert len(store.table("live_prices")) == 1


def test_tick_history_qty_is_relayed(store):
    broker_worker._relay_tick(_tick(qty=5000))

    assert store.table("live_price_ticks")[0]["qty"] == 5000


def test_a_failed_history_write_is_not_swallowed_either(store, monkeypatch):
    calls = []

    def flaky(build):
        calls.append(1)
        if len(calls) == 1:
            return build(_FakeClient(store))
        raise ConnectionError("supabase unreachable")

    monkeypatch.setattr(db, "_run", flaky)

    with pytest.raises(ConnectionError):
        broker_worker._relay_tick(_tick())

    # The cache write (call 1) went through; only the history write (call 2) failed.
    assert store.table("live_prices")


def _old_tick(days_old, code="031234", price=1.0, broker="kgi"):
    ts = datetime.now(timezone.utc) - timedelta(days=days_old)
    return Tick(code=code, price=price, broker=broker, qty=None, ts=ts)


def test_cleanup_drops_ticks_past_the_retention_window(store):
    broker_worker._relay_tick(_old_tick(broker_worker.TICK_RETENTION_DAYS + 1))
    broker_worker._relay_tick(_tick())

    broker_worker._cleanup_tick_history()

    rows = store.table("live_price_ticks")
    assert len(rows) == 1
    assert rows[0]["price"] == 1.23


def test_cleanup_leaves_fresh_ticks_alone(store):
    broker_worker._relay_tick(_tick())

    broker_worker._cleanup_tick_history()

    assert len(store.table("live_price_ticks")) == 1


def test_a_failed_cleanup_is_not_swallowed(store, monkeypatch):
    monkeypatch.setattr(db, "_run", lambda build: (_ for _ in ()).throw(
        ConnectionError("supabase unreachable")))

    with pytest.raises(ConnectionError):
        broker_worker._cleanup_tick_history()


# ── the live-depth relay (#51) ───────────────────────────────────────────

def _depth(code="031234", broker="kgi"):
    return Depth(
        code=code,
        bid_prices=(1.20, 1.19, 1.18, 1.17, 1.16),
        bid_volumes=(10, 20, 30, 40, 50),
        ask_prices=(1.21, 1.22, 1.23, 1.24, 1.25),
        ask_volumes=(5, 15, 25, 35, 45),
        broker=broker,
        ts=datetime(2026, 8, 4, 5, 30, 0, tzinfo=timezone.utc),
    )


def test_a_depth_snapshot_is_written_to_the_live_depth_relay(store):
    broker_worker._relay_depth(_depth())

    assert store.table("live_depth") == [{
        "code": "031234",
        "bid_prices": [1.20, 1.19, 1.18, 1.17, 1.16],
        "bid_volumes": [10, 20, 30, 40, 50],
        "ask_prices": [1.21, 1.22, 1.23, 1.24, 1.25],
        "ask_volumes": [5, 15, 25, 35, 45],
        "ts": "2026-08-04T05:30:00+00:00",
        "broker": "kgi",
    }]


def test_the_depth_relay_keeps_one_row_per_code(store):
    broker_worker._relay_depth(_depth())
    broker_worker._relay_depth(_depth())

    assert len(store.table("live_depth")) == 1


def test_a_failed_depth_write_is_not_swallowed_here(store, monkeypatch):
    monkeypatch.setattr(db, "_run", lambda build: (_ for _ in ()).throw(
        ConnectionError("supabase unreachable")))

    with pytest.raises(ConnectionError):
        broker_worker._relay_depth(_depth())
