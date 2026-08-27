"""Tick-driven dirty tracking for the Live Warrant tab (services/live_warrant.py).

Simulates a websocket tick by calling `_handle_message` directly against a
fake connection and a synthetic JSON frame — no SDK, no live market — then
asserts on the module's in-process state (`_book_seq`, `_computed`,
`_console_log`). This is the one impure-glue path worth a targeted test: the
"deep-level-only change does not dirty" behavior is new and easy to get
subtly wrong (the rest of this module is verified manually against the real
Fubon connection).
"""
import json
import time

import pytest

from services import live_warrant as lw

CODE = "038888"
UNDERLYING = "2330"


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from empty module state and leaves it empty after —
    these are process-wide globals, not per-instance."""
    for d in (lw._books, lw._book_seq, lw._computed,
              lw._terms, lw._underlying_of, lw._underlying_books, lw._underlying_names):
        d.clear()
    lw._underlying_codes.clear()
    lw._console_log.clear()
    lw._console_log_next_id = 0
    lw._last_cpu_throttle["nr"] = None
    lw._last_cpu_throttle["usec"] = None
    yield
    for d in (lw._books, lw._book_seq, lw._computed,
              lw._terms, lw._underlying_of, lw._underlying_books, lw._underlying_names):
        d.clear()
    lw._underlying_codes.clear()
    lw._console_log.clear()
    lw._console_log_next_id = 0
    lw._last_cpu_throttle["nr"] = None
    lw._last_cpu_throttle["usec"] = None


def _frame(bids, asks, code=CODE):
    return json.dumps({
        "event": "data", "channel": lw.BOOKS_CHANNEL,
        "data": {"symbol": code, "bids": bids, "asks": asks},
    })


def test_first_tick_dirties_and_seeds_book():
    lw._handle_message({}, _frame([{"price": 10.0, "size": 5}], [{"price": 10.5, "size": 3}]))
    assert lw._book_seq[CODE] == 1
    assert lw._books[CODE]["bids"] == [{"price": 10.0, "size": 5}]


def test_best_level_move_dirties_again():
    lw._handle_message({}, _frame([{"price": 10.0, "size": 5}], [{"price": 10.5, "size": 3}]))
    lw._handle_message({}, _frame([{"price": 10.1, "size": 5}], [{"price": 10.5, "size": 3}]))
    assert lw._book_seq[CODE] == 2


def test_deep_level_only_move_does_not_dirty():
    """The whole point of the dirty gate: a level-2+ requote that leaves the
    best level untouched must not bump the sequence."""
    lw._handle_message({}, _frame(
        [{"price": 10.0, "size": 5}, {"price": 9.9, "size": 1}], []))
    seq_after_first = lw._book_seq[CODE]
    lw._handle_message({}, _frame(
        [{"price": 10.0, "size": 5}, {"price": 9.5, "size": 40}], []))
    assert lw._book_seq[CODE] == seq_after_first


def test_underlying_tick_routes_to_underlying_books_not_warrant_books():
    lw._underlying_codes.add(UNDERLYING)
    lw._handle_message({}, _frame(
        [{"price": 600.0, "size": 10}], [{"price": 600.5, "size": 8}], code=UNDERLYING))
    assert UNDERLYING in lw._underlying_books
    assert lw._underlying_books[UNDERLYING]["price"] == pytest.approx(600.25)
    assert lw._underlying_books[UNDERLYING]["best"] == {
        "bid": 600.0, "bid_size": 10, "ask": 600.5, "ask_size": 8}
    assert UNDERLYING not in lw._books
    assert UNDERLYING not in lw._book_seq


# ── underlying display row (appears at the top, before its warrant chain) ──

def test_underlying_row_payload_pending_before_first_tick():
    lw._underlying_codes.add(UNDERLYING)
    lw._underlying_names[UNDERLYING] = "台積電"
    row = lw._underlying_row_payload(UNDERLYING)
    assert row["code"] == UNDERLYING
    assert row["name"] == "台積電"
    assert row["pending"] is True
    assert row["type"] == "Underlying"
    assert row["underlying_price"] is None
    # Warrant-only fields must degrade to None/"—", never raise or fabricate a value.
    assert row["strike"] is None and row["dte"] is None


def test_underlying_row_payload_fills_in_after_a_tick():
    lw._underlying_codes.add(UNDERLYING)
    lw._underlying_names[UNDERLYING] = "台積電"
    lw._handle_message({}, _frame(
        [{"price": 600.0, "size": 10}], [{"price": 600.5, "size": 8}], code=UNDERLYING))
    row = lw._underlying_row_payload(UNDERLYING)
    assert row["pending"] is False
    assert row["underlying_price"] == pytest.approx(600.25)
    assert row["best"] == {"bid": 600.0, "bid_size": 10, "ask": 600.5, "ask_size": 8}


def test_underlying_row_falls_back_to_code_when_name_unknown():
    lw._underlying_codes.add(UNDERLYING)
    row = lw._underlying_row_payload(UNDERLYING)
    assert row["name"] == UNDERLYING


def test_get_data_lists_underlying_rows_before_tracked_warrants():
    """Subscribing the underlying before its chain (scan_underlying) is only
    half the point — its row has to actually render first too."""
    lw._underlying_codes.add(UNDERLYING)
    lw._underlying_names[UNDERLYING] = "台積電"
    lw._tracked.append(CODE)
    try:
        d = lw.get_data()
        assert [b["code"] for b in d["books"]] == [UNDERLYING, CODE]
        # The underlying must never be counted as a tracked warrant — it has
        # no terms/volume/capacity accounting of its own.
        assert d["tracked"] == 1
    finally:
        lw._tracked.remove(CODE)


def test_recompute_if_dirty_no_op_when_terms_missing():
    """Missing inputs must leave the cached value untouched, not raise."""
    lw._handle_message({}, _frame([{"price": 10.0, "size": 5}], [{"price": 10.5, "size": 3}]))
    lw._recompute_if_dirty(CODE)
    assert CODE not in lw._computed


def test_recompute_if_dirty_computes_once_inputs_present():
    lw._underlying_codes.add(UNDERLYING)
    lw._terms[CODE] = {"strike": 590.0, "exercise_ratio": 0.1, "maturity": None}
    lw._underlying_of[CODE] = UNDERLYING
    lw._underlying_books[UNDERLYING] = {"price": 620.0, "ts": None, "src": "ws"}
    lw._names[CODE] = "台積電元大11購01"  # -> Call

    lw._handle_message({}, _frame([{"price": 3.2, "size": 5}], [{"price": 3.4, "size": 3}]))
    lw._recompute_if_dirty(CODE)

    assert CODE in lw._computed
    assert lw._computed[CODE]["seq"] == lw._book_seq[CODE]
    assert lw._computed[CODE]["time_value"] is not None


def test_recompute_if_dirty_is_a_no_op_once_current():
    lw._underlying_codes.add(UNDERLYING)
    lw._terms[CODE] = {"strike": 590.0, "exercise_ratio": 0.1, "maturity": None}
    lw._underlying_of[CODE] = UNDERLYING
    lw._underlying_books[UNDERLYING] = {"price": 620.0, "ts": None, "src": "ws"}
    lw._names[CODE] = "台積電元大11購01"

    lw._handle_message({}, _frame([{"price": 3.2, "size": 5}], [{"price": 3.4, "size": 3}]))
    lw._recompute_if_dirty(CODE)
    first = dict(lw._computed[CODE])

    lw._recompute_if_dirty(CODE)  # nothing changed since — must be a no-op
    assert lw._computed[CODE] == first


def test_recompute_if_dirty_nan_result_sanitized_to_none_for_json():
    """A bare NaN literal from jsonify breaks JSON.parse client-side — an
    ask-only warrant (no bid) means bid_time_value_pct is NaN internally and
    must come out of _computed as None, not float('nan')."""
    lw._underlying_codes.add(UNDERLYING)
    lw._terms[CODE] = {"strike": 590.0, "exercise_ratio": 0.1, "maturity": None}
    lw._underlying_of[CODE] = UNDERLYING
    lw._underlying_books[UNDERLYING] = {"price": 620.0, "ts": None, "src": "ws"}
    lw._names[CODE] = "台積電元大11購01"

    lw._handle_message({}, _frame([], [{"price": 3.4, "size": 3}]))  # no bid
    lw._recompute_if_dirty(CODE)

    assert lw._computed[CODE]["bid_time_value_pct"] is None
    assert lw._computed[CODE]["time_value"] is not None
    import json
    json.dumps(lw._computed[CODE])  # must not raise / must not embed a bare NaN
    assert "NaN" not in json.dumps(lw._computed[CODE])


def test_recompute_if_dirty_expired_warrant_stays_uncomputed():
    import datetime as dt
    lw._underlying_codes.add(UNDERLYING)
    lw._terms[CODE] = {
        "strike": 590.0, "exercise_ratio": 0.1,
        "maturity": dt.date.today() - dt.timedelta(days=1),  # expired yesterday
    }
    lw._underlying_of[CODE] = UNDERLYING
    lw._underlying_books[UNDERLYING] = {"price": 620.0, "ts": None, "src": "ws"}
    lw._names[CODE] = "台積電元大11購01"

    lw._handle_message({}, _frame([{"price": 3.2, "size": 5}], [{"price": 3.4, "size": 3}]))
    lw._recompute_if_dirty(CODE)
    assert CODE not in lw._computed


# ── _fan_out: bounded wall-clock, not just bounded parallelism ─────────────
#
# Parallelizing retry_pending()'s REST-bound loops (SEED_WORKERS threads
# instead of one code at a time) was the first fix for "Retry Pending
# freezes" — but pool.map() still blocks the caller until every submitted
# call finishes, and one quota-starved call can itself take ~60s+. A big
# backlog could still make that "parallel" version exceed the request
# timeout and get killed mid-batch, which is the second half of the bug
# report. _fan_out is what actually bounds the CALLER's wall-clock time,
# regardless of how long individual tasks take.

def test_fan_out_returns_by_the_deadline_even_with_slow_tasks():
    def _slow(code):
        time.sleep(2)
        return True

    t0 = time.monotonic()
    lw._fan_out(_slow, ["A", "B", "C"], deadline=time.monotonic() + 0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"_fan_out blocked past its deadline: took {elapsed:.2f}s"


def test_fan_out_counts_only_what_finished_before_the_deadline():
    def _slow(code):
        time.sleep(2)
        return True

    done = lw._fan_out(_slow, ["A", "B"], deadline=time.monotonic() + 0.2)
    assert done == 0  # neither task could have finished in 0.2s


def test_fan_out_returns_completed_count_when_tasks_are_fast():
    done = lw._fan_out(lambda code: True, ["A", "B", "C"], deadline=time.monotonic() + 5)
    assert done == 3


def test_fan_out_does_not_count_a_false_result():
    done = lw._fan_out(lambda code: code == "A", ["A", "B"], deadline=time.monotonic() + 5)
    assert done == 1


def test_fan_out_empty_codes_is_a_no_op():
    assert lw._fan_out(lambda code: True, [], deadline=time.monotonic() + 5) == 0


# ── _fan_out_start/_fan_out_finish: split submit-from-wait ──────────────────
#
# retry_pending() launches its reseed and terms batches via this split form
# so both start running before either is awaited — see retry_pending's own
# docstring for the production freeze this fixes (a slow reseed batch ate
# 53 of a 60s budget and left the terms batch only 6.6s, even though both
# draw from the same REST quota regardless of which runs "first").

def test_fan_out_start_then_finish_matches_fan_out_in_one_shot():
    pool, futures = lw._fan_out_start(lambda code: code == "A", ["A", "B"],
                                       deadline=time.monotonic() + 5)
    done = lw._fan_out_finish(lambda code: None, pool, futures, deadline=time.monotonic() + 5)
    assert done == 1


def test_fan_out_start_empty_codes_returns_no_pool():
    pool, futures = lw._fan_out_start(lambda code: True, [], deadline=time.monotonic() + 5)
    assert pool is None and futures == []
    assert lw._fan_out_finish(lambda code: None, pool, futures, deadline=time.monotonic() + 5) == 0


def test_fan_out_start_runs_concurrently_before_being_awaited():
    """Two batches submitted via _fan_out_start must overlap in wall time —
    the whole point of splitting submit from wait."""
    started = []

    def _slow(code):
        started.append((code, time.monotonic()))
        time.sleep(0.3)
        return True

    deadline = time.monotonic() + 5
    pool_a, futures_a = lw._fan_out_start(_slow, ["A"], deadline)
    pool_b, futures_b = lw._fan_out_start(_slow, ["B"], deadline)
    lw._fan_out_finish(_slow, pool_a, futures_a, deadline)
    lw._fan_out_finish(_slow, pool_b, futures_b, deadline)

    (_, ts_a), (_, ts_b) = started
    assert abs(ts_a - ts_b) < 0.2, "second batch did not start until the first was awaited"


# ── _RestQuota: a 60s budget plus a much shorter burst cap ──────────────────
#
# The 300/60s cap alone only bounds the average rate: right after a window
# with slack, many threads can see room and fire in the same instant, which
# is the exact "8 unbounded workers -> real 429s" case the module's own
# comment documents even though the total stayed under budget — and those
# same simultaneous hits later age out of the 60s window together too,
# re-bursting all over again. The short burst window caps how many can be
# granted within any one second; a first-attempt fix that instead paced
# EVERY grant down to the full 60s average measurably regressed throughput
# (an ~11s stage became ~40s) for no proven benefit over capping the burst.

def test_rest_quota_denies_past_the_sliding_window_limit():
    quota = lw._RestQuota(limit=2, window=60.0, burst_limit=10, burst_window=1.0)
    assert quota.acquire(timeout=0) is True
    assert quota.acquire(timeout=0) is True
    assert quota.acquire(timeout=0) is False  # third slot not free yet


def test_rest_quota_denies_past_the_burst_limit_even_with_window_room():
    quota = lw._RestQuota(limit=100, window=60.0, burst_limit=2, burst_window=1.0)
    assert quota.acquire(timeout=0) is True
    assert quota.acquire(timeout=0) is True
    assert quota.acquire(timeout=0) is False  # burst cap hit, plenty of window room left


def test_rest_quota_burst_slot_frees_once_the_burst_window_elapses():
    quota = lw._RestQuota(limit=100, window=60.0, burst_limit=1, burst_window=0.1)
    assert quota.acquire(timeout=0) is True
    assert quota.acquire(timeout=1) is True  # waits out the burst window, then succeeds
    elapsed_ok = quota.used() == 2
    assert elapsed_ok


def test_rest_quota_used_reports_current_window_count():
    quota = lw._RestQuota(limit=5, window=60.0, burst_limit=10, burst_window=1.0)
    quota.acquire()
    quota.acquire()
    assert quota.used() == 2


# ── _preopen_connections: no-op when there's already enough capacity ───────

def test_preopen_connections_noop_when_capacity_already_sufficient():
    """Must not touch `_connections` (and therefore never call the real
    SDK/login path) when existing connections already cover the target."""
    fake_conns = [{"codes": set(range(300))}, {"codes": set(range(300))}]
    lw._connections.extend(fake_conns)
    try:
        lw._preopen_connections(target_total_subs=500)  # fits in 2 connections
        assert lw._connections == fake_conns  # unchanged
    finally:
        lw._connections.clear()


# ── _log_debug: freeze-diagnostic checkpoints ───────────────────────────────

def test_log_debug_appends_a_debug_level_entry():
    lw._console_log.clear()
    before_id = lw._console_log_next_id
    lw._log_debug("test checkpoint")
    entry = lw._console_log[-1]
    assert entry["level"] == "debug"
    assert entry["diff"] == "test checkpoint"
    assert entry["code"] == "-"
    assert entry["id"] == before_id + 1
    lw._console_log.clear()


def test_log_debug_is_retrievable_via_get_console_log():
    lw._console_log.clear()
    since = lw._console_log_next_id
    lw._log_debug("checkpoint one")
    lw._log_debug("checkpoint two")
    result = lw.get_console_log(since=since)
    assert [e["diff"] for e in result["entries"]] == ["checkpoint one", "checkpoint two"]
    assert all(e["level"] == "debug" for e in result["entries"])
    lw._console_log.clear()


# ── _log_resource_snapshot: memory/CPU-limit diagnostics ────────────────────
#
# No real cgroup exists on this machine (confirmed: memlog's readers already
# return None gracefully outside a container), so these tests exercise the
# graceful-degradation path plus the throttle-delta math against a mocked
# memlog, not real /sys/fs/cgroup numbers — that only exists on the real
# Render deployment this feature is meant to diagnose.

def test_resource_snapshot_reports_unavailable_with_no_cgroup(monkeypatch):
    from services import memlog
    monkeypatch.setattr(memlog, "memory_usage_fraction", lambda: None)
    monkeypatch.setattr(memlog, "cpu_quota_vcpus", lambda: None)
    monkeypatch.setattr(memlog, "cpu_throttle_stats", lambda: (None, None))
    lw._console_log.clear()
    lw._log_resource_snapshot("test")
    msg = lw._console_log[-1]["diff"]
    assert "memory limit unavailable" in msg
    assert "CPU throttle stats unavailable" in msg
    lw._console_log.clear()


def test_resource_snapshot_flags_high_memory_as_critical(monkeypatch):
    from services import memlog
    monkeypatch.setattr(memlog, "memory_usage_fraction", lambda: 0.95)
    monkeypatch.setattr(memlog, "cpu_quota_vcpus", lambda: 1.0)
    monkeypatch.setattr(memlog, "cpu_throttle_stats", lambda: (0, 0))
    lw._console_log.clear()
    lw._last_cpu_throttle["nr"] = None
    lw._last_cpu_throttle["usec"] = None
    lw._log_resource_snapshot("test")
    msg = lw._console_log[-1]["diff"]
    assert "95.0%" in msg
    assert "CRITICAL" in msg
    lw._console_log.clear()


def test_resource_snapshot_detects_new_cpu_throttling_since_last_check(monkeypatch):
    from services import memlog
    monkeypatch.setattr(memlog, "memory_usage_fraction", lambda: 0.5)
    monkeypatch.setattr(memlog, "cpu_quota_vcpus", lambda: 1.0)
    lw._console_log.clear()
    lw._last_cpu_throttle["nr"] = None
    lw._last_cpu_throttle["usec"] = None

    monkeypatch.setattr(memlog, "cpu_throttle_stats", lambda: (5, 200_000))
    lw._log_resource_snapshot("first")
    assert "THROTTLED" not in lw._console_log[-1]["diff"]  # nothing to diff against yet

    monkeypatch.setattr(memlog, "cpu_throttle_stats", lambda: (8, 350_000))
    lw._log_resource_snapshot("second")
    msg = lw._console_log[-1]["diff"]
    assert "THROTTLED 3 more time(s)" in msg
    assert "150ms" in msg
    lw._console_log.clear()


def test_resource_snapshot_no_new_throttling_reads_clean(monkeypatch):
    from services import memlog
    monkeypatch.setattr(memlog, "memory_usage_fraction", lambda: 0.3)
    monkeypatch.setattr(memlog, "cpu_quota_vcpus", lambda: 1.0)
    monkeypatch.setattr(memlog, "cpu_throttle_stats", lambda: (5, 200_000))
    lw._console_log.clear()
    lw._last_cpu_throttle["nr"] = 5
    lw._last_cpu_throttle["usec"] = 200_000
    lw._log_resource_snapshot("test")
    msg = lw._console_log[-1]["diff"]
    assert "no new throttling" in msg
    lw._console_log.clear()
