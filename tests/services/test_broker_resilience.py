"""Reconnect-with-backoff, worker_status transitions, and the alert gate.

No broker and no network: the pool is a stub, and time is injected so the whole
10-attempt ladder runs instantly while still being measured in real seconds.
"""
import pytest

from services.broker import resilience


class FakeClock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


class FakePool:
    def __init__(self, connected=True):
        self.clients = [_FakeClient(connected)]
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, connected):
        self.is_connected = connected


def build(open_results, clock=None):
    """Supervisor whose pool opens per `open_results` (a pool, or an Exception)."""
    clock = clock or FakeClock()
    calls = []
    statuses = []
    alerted = []

    def open_pool():
        calls.append(1)
        outcome = open_results.pop(0) if open_results else FakePool()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sup = resilience.SessionSupervisor(
        open_pool=open_pool,
        on_status=statuses.append,
        alert=alerted.append,
        sleep=clock.sleep,
        clock=clock,
    )
    return sup, statuses, alerted, clock, calls


def test_backoff_ladder_doubles_to_cap():
    delays = list(resilience.backoff_delays())
    assert len(delays) == resilience.MAX_RECONNECT_ATTEMPTS
    assert delays[:5] == [5.0, 10.0, 20.0, 40.0, 60.0]
    assert set(delays[4:]) == {60.0}


def test_start_writes_connected():
    sup, statuses, alerted, _clock, _calls = build([FakePool()])
    sup.start()
    assert statuses == ["connected"]
    assert alerted == []
    assert sup.running


def test_healthy_pool_is_left_alone():
    sup, statuses, _alerted, _clock, calls = build([FakePool()])
    sup.start()
    assert sup.check_health() is True
    assert statuses == ["connected"]
    assert len(calls) == 1


def test_drop_recovers_on_first_attempt_without_alerting():
    # Dropped pool, then a healthy one on the first reconnect attempt (t=5s,
    # inside the grace window).
    sup, statuses, alerted, clock, _calls = build([FakePool(connected=False), FakePool()])
    sup.start()
    assert sup.check_health() is True
    assert statuses == ["connected", "reconnecting", "connected"]
    assert alerted == []
    assert clock.slept == [5.0]


def test_prolonged_outage_alerts_once_then_recovers():
    # Fails the first two reconnect attempts (t=5s, t=15s), succeeds on the
    # third — the alert fires when the incident crosses the grace threshold.
    sup, statuses, alerted, clock, _calls = build([
        FakePool(connected=False),
        RuntimeError("broker down"),
        RuntimeError("broker down"),
        FakePool(),
    ])
    sup.start()
    sup.check_health()
    assert statuses == ["connected", "reconnecting", "connected"]
    assert len(alerted) == 1
    assert clock.now >= resilience.ALERT_AFTER_SEC


def test_exhausted_ladder_lands_on_disconnected_with_one_alert():
    failures = [FakePool(connected=False)] + [RuntimeError("down")] * 20
    sup, statuses, alerted, _clock, calls = build(failures)
    sup.start()
    assert sup.check_health() is False
    assert statuses == ["connected", "reconnecting", "disconnected"]
    # One alert for the whole incident: reconnecting -> disconnected must not
    # page a second time.
    assert len(alerted) == 1
    # One open at start plus exactly MAX_RECONNECT_ATTEMPTS retries.
    assert len(calls) == 1 + resilience.MAX_RECONNECT_ATTEMPTS
    assert not sup.running


def test_stop_writes_stopped_directly_and_never_alerts():
    pool = FakePool()
    sup, statuses, alerted, _clock, _calls = build([pool])
    sup.start()
    sup.stop()
    assert statuses == ["connected", "stopped"]
    assert "reconnecting" not in statuses
    assert alerted == []
    assert pool.closed
    assert not sup.running


def test_stop_after_disconnect_is_still_a_stop():
    sup, statuses, alerted, _clock, _calls = build(
        [FakePool(connected=False)] + [RuntimeError("down")] * 20)
    sup.start()
    sup.check_health()
    sup.stop()
    assert statuses[-2:] == ["disconnected", "stopped"]
    assert len(alerted) == 1


def test_health_check_is_a_noop_when_not_running():
    sup, statuses, _alerted, _clock, calls = build([FakePool()])
    assert sup.check_health() is True
    assert statuses == []
    assert calls == []


def test_failed_start_enters_the_same_ladder():
    sup, statuses, _alerted, _clock, _calls = build([RuntimeError("no route"), FakePool()])
    assert sup.start() is True
    assert statuses == ["reconnecting", "connected"]


def test_status_write_failure_does_not_abort_reconnect():
    clock = FakeClock()
    pools = [FakePool(connected=False), FakePool()]
    seen = []

    def on_status(status):
        seen.append(status)
        raise RuntimeError("supabase down")

    sup = resilience.SessionSupervisor(
        open_pool=lambda: pools.pop(0),
        on_status=on_status,
        alert=None,
        sleep=clock.sleep,
        clock=clock,
    )
    sup.start()
    assert sup.check_health() is True
    assert seen == ["connected", "reconnecting", "connected"]


def test_alert_failure_does_not_abort_reconnect():
    clock = FakeClock()
    pools = [FakePool(connected=False), RuntimeError("down"), RuntimeError("down"), FakePool()]

    def open_pool():
        out = pools.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def alert(_reason):
        raise RuntimeError("slack down")

    sup = resilience.SessionSupervisor(
        open_pool=open_pool, on_status=lambda s: None, alert=alert,
        sleep=clock.sleep, clock=clock,
    )
    sup.start()
    assert sup.check_health() is True
