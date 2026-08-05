"""Worker reconciliation: desired state in, broker logins and status rows out.

Nothing real is touched — no broker, no network, no Supabase. The faked
boundaries are the broker client classes (stand-ins that record login/logout)
and the two desired_state calls the worker makes, which is the whole surface the
worker has in this ticket: it logs in, it logs out, it reports what happened.

The real SDKs are deliberately not importable here (kgisuperpy / fubon_neo ship
only in the worker image), which is exactly why broker_worker imports the client
modules inside client_class() rather than at module scope. One test below pins
that down by injecting stub SDK modules and checking the dispatch resolves.
"""
import sys
import types
from unittest.mock import patch

import pytest

import broker_worker
from services.broker.base import BrokerConnectionError


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
    assert intervals == [broker_worker.DESIRED_POLL_SEC, broker_worker.HEARTBEAT_SEC]


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
