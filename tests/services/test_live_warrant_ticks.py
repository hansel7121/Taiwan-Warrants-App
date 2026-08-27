"""Tick-driven dirty tracking for the Live Warrant tab (services/live_warrant.py).

Simulates a websocket tick by calling `_handle_message` directly against a
fake connection and a synthetic JSON frame — no SDK, no live market — then
asserts on the module's in-process state (`_book_seq`, `_computed`,
`_console_log`). This is the one impure-glue path worth a targeted test: the
"deep-level-only change does not dirty" behavior is new and easy to get
subtly wrong, and the console-log queue/drain interaction between
`_handle_message` and `_recompute_if_dirty` has no other safety net (the rest
of this module is verified manually against the real Fubon connection).
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
    for d in (lw._books, lw._book_seq, lw._computed, lw._pending_log,
              lw._terms, lw._underlying_of, lw._underlying_books, lw._underlying_names):
        d.clear()
    lw._underlying_codes.clear()
    lw._console_log.clear()
    lw._console_log_next_id = 0
    lw._last_cpu_throttle["nr"] = None
    lw._last_cpu_throttle["usec"] = None
    yield
    for d in (lw._books, lw._book_seq, lw._computed, lw._pending_log,
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
    assert len(lw._pending_log[CODE]) == 1
    assert lw._pending_log[CODE][0]["diff"].startswith("seeded:")


def test_best_level_move_dirties_again():
    lw._handle_message({}, _frame([{"price": 10.0, "size": 5}], [{"price": 10.5, "size": 3}]))
    lw._handle_message({}, _frame([{"price": 10.1, "size": 5}], [{"price": 10.5, "size": 3}]))
    assert lw._book_seq[CODE] == 2
    assert len(lw._pending_log[CODE]) == 2


def test_deep_level_only_move_does_not_dirty():
    """The whole point of the dirty gate: a level-2+ requote that leaves the
    best level untouched must not bump the sequence or queue a log entry."""
    lw._handle_message({}, _frame(
        [{"price": 10.0, "size": 5}, {"price": 9.9, "size": 1}], []))
    seq_after_first = lw._book_seq[CODE]
    lw._handle_message({}, _frame(
        [{"price": 10.0, "size": 5}, {"price": 9.5, "size": 40}], []))
    assert lw._book_seq[CODE] == seq_after_first
    assert len(lw._pending_log[CODE]) == 1  # only the first (seeding) tick queued


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
    # The queued diff must still be there, waiting for the input to show up —
    # not silently dropped.
    assert len(lw._pending_log[CODE]) == 1


def test_recompute_if_dirty_computes_and_logs_once_inputs_present():
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
    assert len(lw._console_log) == 1
    entry = lw._console_log[0]
    assert entry["code"] == CODE
    assert "time_value" in entry["recalculated"]
    assert entry["duration_s"] >= 0
    assert lw._pending_log.get(CODE) is None  # drained


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
    assert len(lw._console_log) == 1  # no duplicate log entry


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
