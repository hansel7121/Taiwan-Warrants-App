"""Worker wiring that does not need a broker, a network, or Supabase: the tick
pipeline's translate -> check -> append path, and the reconcile rule that both
market hours and user intent must agree before any session opens.

The scheduler itself is not exercised here — job registration is verified by
reading build_scheduler, and the live path end-to-end belongs to the tick-replay
harness (#32).
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

import broker_worker
from logic import arb_logic
from services.broker.base import Tick


TICK = Tick(code="071234", price=2.5, ts=datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc),
            broker="kgi")

SNAPSHOT_ROW = {
    "warrant_code": "071234",
    "warrant_name": "TSMC Call 01",
    "underlying_code": "2330",
    "type": "Call",
    "underlying_price": 1000.0,
    "ask": 2.0,
    "bid": 1.9,
    "days_to_expiry": 30,
    "strike": 1000.0,
    "exercise_ratio": 0.5,
}


@pytest.fixture
def worker(monkeypatch):
    w = broker_worker.Worker()
    w._warrant_rows = {"071234": dict(SNAPSHOT_ROW)}
    return w


def test_tick_produces_signals_from_the_registered_strategies(worker, monkeypatch):
    matches = [{"warrant_code": "071234", "option_contract": "OPT1",
                "price_diff": 1.0, "price_diff_pct": 2.0, "strategy": "same_type"}]
    monkeypatch.setattr(arb_logic, "check_tick", lambda row, m, *a, **kw: matches)
    written = []
    monkeypatch.setattr(broker_worker.db_signals, "insert_signals",
                        lambda rows: written.extend(rows) or len(rows))

    assert worker.handle_tick(TICK) == 1
    assert written[0]["warrant_code"] == "071234"
    assert written[0]["tick_broker"] == "kgi"
    assert written[0]["tick_ts"] == TICK.ts.isoformat()


def test_the_tick_price_is_what_the_matcher_sees(worker, monkeypatch):
    seen = {}

    def capture(row, _mirror, *a, **kw):
        seen.update(row)
        return []

    monkeypatch.setattr(arb_logic, "check_tick", capture)
    worker.handle_tick(TICK)

    # tick_translate puts the print on the ask (warrants are bought) and voids
    # the stale book side.
    assert seen["ask"] == 2.5
    assert seen["bid"] == 0.0


def test_a_tick_with_no_snapshot_row_is_dropped_not_guessed(worker, monkeypatch):
    monkeypatch.setattr(arb_logic, "check_tick",
                        lambda *a, **kw: pytest.fail("should not match"))
    unknown = Tick(code="099999", price=1.0, ts=TICK.ts, broker="kgi")
    assert worker.handle_tick(unknown) == 0


def test_no_matches_writes_nothing(worker, monkeypatch):
    monkeypatch.setattr(arb_logic, "check_tick", lambda *a, **kw: [])
    monkeypatch.setattr(broker_worker.db_signals, "insert_signals",
                        lambda rows: pytest.fail("should not write"))
    assert worker.handle_tick(TICK) == 0


def test_ticks_are_only_ever_queued_never_stored(worker):
    worker._on_tick(TICK)
    assert worker._ticks.qsize() == 1
    # Nothing persisted the raw print (docs/adr/0003) — the queue is the only
    # place it exists, and it is bounded.
    assert worker._ticks.maxsize == broker_worker.TICK_QUEUE_MAX


def test_a_full_queue_drops_rather_than_growing(worker):
    worker._ticks.maxsize = 2
    for _ in range(5):
        worker._on_tick(TICK)
    assert worker._ticks.qsize() == 2
    assert worker._dropped == 3


# ── reconcile ────────────────────────────────────────────────────────────

class _FakeSupervisor:
    def __init__(self):
        self.running = False
        self.events = []

    def start(self):
        self.events.append("start")
        self.running = True

    def stop(self):
        self.events.append("stop")
        self.running = False


def _with_accounts(monkeypatch, users):
    class _Account:
        def __init__(self, user_id):
            self.user_id = user_id
            self.broker = "kgi"

    monkeypatch.setattr(broker_worker.broker_pool, "accounts_for",
                        lambda ids: [_Account(u) for u in ids if u in users])


def test_no_session_opens_while_the_market_is_closed(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1"})
    worker._wanted = {"u1"}
    worker._market_open = False

    worker.reconcile()
    assert worker.supervisor.events == []


def test_wanting_connect_during_hours_opens_a_session(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1"})
    worker._wanted = {"u1"}
    worker._market_open = True

    worker.reconcile()
    assert worker.supervisor.events == ["start"]
    assert worker._accounts == [("u1", "kgi")]


def test_market_close_stops_the_session(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1"})
    worker._wanted = {"u1"}
    worker._market_open = True
    worker.reconcile()

    worker._market_open = False
    worker.reconcile()
    # 'stopped' via supervisor.stop(), never a reconnect — an expected close.
    assert worker.supervisor.events == ["start", "stop"]


def test_withdrawing_intent_stops_the_session(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1"})
    worker._wanted = {"u1"}
    worker._market_open = True
    worker.reconcile()

    worker._wanted = set()
    worker.reconcile()
    assert worker.supervisor.events == ["start", "stop"]


def test_reconcile_is_idempotent(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1"})
    worker._wanted = {"u1"}
    worker._market_open = True

    worker.reconcile()
    worker.reconcile()
    worker.reconcile()
    assert worker.supervisor.events == ["start"]


def test_a_second_user_connecting_reopens_the_pool_with_both(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, {"u1", "u2"})
    worker._wanted = {"u1"}
    worker._market_open = True
    worker.reconcile()

    worker._wanted = {"u1", "u2"}
    worker.reconcile()
    assert worker.supervisor.events == ["start", "stop", "start"]
    assert sorted(u for u, _b in worker._accounts) == ["u1", "u2"]


def test_intent_without_stored_credentials_opens_nothing(worker, monkeypatch):
    worker.supervisor = _FakeSupervisor()
    _with_accounts(monkeypatch, set())
    worker._wanted = {"u1"}
    worker._market_open = True

    worker.reconcile()
    assert worker.supervisor.events == []
    assert worker._accounts == []


def test_status_fans_out_to_every_account_of_the_pool(worker, monkeypatch):
    writes = []
    monkeypatch.setattr(broker_worker.desired_state, "set_worker_status",
                        lambda u, b, s: writes.append((u, b, s)))
    worker._accounts = [("u1", "kgi"), ("u2", "fubon")]

    worker._write_status("connected")
    assert writes == [("u1", "kgi", "connected"), ("u2", "fubon", "connected")]


# ── mirrors ──────────────────────────────────────────────────────────────

def test_refresh_keeps_the_old_warrant_rows_when_a_read_comes_back_empty(worker, monkeypatch):
    monkeypatch.setattr(broker_worker.mirror, "load_option_mirror",
                        lambda: (arb_logic.OptionMirror(), None))
    monkeypatch.setattr(broker_worker.mirror, "load_warrant_rows", lambda: ({}, None))

    worker.refresh_mirrors()
    # A transiently empty snapshot must not blind every subsequent tick.
    assert "071234" in worker._warrant_rows


def test_refresh_swaps_in_a_new_snapshot(worker, monkeypatch):
    fresh = {"071999": dict(SNAPSHOT_ROW, warrant_code="071999")}
    monkeypatch.setattr(broker_worker.mirror, "load_option_mirror",
                        lambda: (arb_logic.OptionMirror(pd.DataFrame()), "t0"))
    monkeypatch.setattr(broker_worker.mirror, "load_warrant_rows", lambda: (fresh, "t0"))

    worker.refresh_mirrors()
    assert list(worker._warrant_rows) == ["071999"]
