"""TTLCache unit tests — pure in-memory logic, no I/O and nothing mocked."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from logic import ttl_cache
from logic.ttl_cache import TTLCache


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts from an empty registry so stats() sees only its caches."""
    ttl_cache.reset_registry()
    yield
    ttl_cache.reset_registry()


def test_get_or_set_caches_the_first_value():
    calls = []
    c = TTLCache("t", ttl=60)

    assert c.get_or_set("k", lambda: calls.append(1) or "v") == "v"
    assert c.get_or_set("k", lambda: calls.append(1) or "other") == "v"
    assert len(calls) == 1


def test_value_expires_after_ttl():
    calls = []

    def load():
        calls.append(1)
        return len(calls)

    c = TTLCache("t", ttl=0.05)
    assert c.get_or_set("k", load) == 1
    time.sleep(0.08)
    assert c.get_or_set("k", load) == 2
    assert len(calls) == 2


def test_force_bypasses_a_live_entry():
    calls = []
    c = TTLCache("t", ttl=60)
    c.get_or_set("k", lambda: calls.append(1) or "a")
    assert c.get_or_set("k", lambda: calls.append(1) or "b", force=True) == "b"
    assert len(calls) == 2
    # the forced value replaced the cached one
    assert c.get_or_set("k", lambda: "c") == "b"


def test_invalidate_one_key_leaves_the_others():
    c = TTLCache("t", ttl=60)
    c.set("a", 1)
    c.set("b", 2)

    assert c.invalidate("a") == 1
    assert c.entry("a") is None
    assert c.entry("b")[1] == 2
    assert len(c) == 1


def test_invalidate_without_key_clears_everything():
    c = TTLCache("t", ttl=60)
    c.set("a", 1)
    c.set("b", 2)

    assert c.invalidate() == 2
    assert len(c) == 0
    assert c.entry("b") is None


def test_invalidate_missing_key_is_a_no_op():
    c = TTLCache("t", ttl=60)
    c.set("a", 1)
    assert c.invalidate("nope") == 0
    assert len(c) == 1


def test_entry_returns_stale_values_but_fresh_does_not():
    c = TTLCache("t", ttl=0.05)
    c.set("k", "v")
    time.sleep(0.08)
    ts, value = c.entry("k")
    assert value == "v" and ts > 0
    assert c.fresh("k") is None


def test_set_accepts_an_explicit_timestamp():
    c = TTLCache("t", ttl=60)
    c.set("k", "v", ts=123.0)
    assert c.entry("k") == (123.0, "v")


def test_items_can_include_or_exclude_expired_entries():
    c = TTLCache("t", ttl=0.05)
    c.set("old", 1)
    time.sleep(0.08)
    c.set("new", 2)

    assert {k for k, _ts, _v in c.items()} == {"old", "new"}
    assert {k for k, _ts, _v in c.items(include_expired=False)} == {"new"}


def test_concurrent_get_or_set_loads_once_per_key():
    calls = []
    lock = threading.Lock()
    c = TTLCache("t", ttl=60)

    def load():
        with lock:
            calls.append(1)
        time.sleep(0.05)  # wide enough for every thread to pile up on the miss
        return "v"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: c.get_or_set("k", load), range(8)))

    assert results == ["v"] * 8
    assert len(calls) == 1


def test_concurrent_loads_of_different_keys_are_not_serialised():
    c = TTLCache("t", ttl=60)
    started = threading.Barrier(4, timeout=2)

    def load():
        # Deadlocks (raising BrokenBarrierError) if one key's load blocks another's.
        started.wait()
        return "v"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: c.get_or_set(f"k{i}", load), range(4)))

    assert results == ["v"] * 4


def test_stats_reports_entry_counts_per_named_cache():
    a = TTLCache("alpha", ttl=60)
    b = TTLCache("beta", ttl=60)
    a.set("1", "x")
    a.set("2", "y")
    b.set("1", "z")

    by_name = {s["name"]: s for s in ttl_cache.stats()}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["entries"] == 2
    assert by_name["beta"]["entries"] == 1
    assert by_name["alpha"]["ttl"] == 60
    assert by_name["alpha"]["bytes"] > 0

    a.invalidate()
    assert {s["name"]: s["entries"] for s in ttl_cache.stats()}["alpha"] == 0


def test_format_stats_mentions_every_cache_and_a_total():
    a = TTLCache("alpha", ttl=60)
    TTLCache("beta", ttl=60)
    a.set("1", "x")

    line = ttl_cache.format_stats()
    assert "alpha" in line and "beta" in line and "total" in line
