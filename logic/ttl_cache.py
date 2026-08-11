"""One thread-safe TTL cache class, shared by every in-process market-data
cache in logic/ (warrant/options/us_options). Two locks per instance: `_lock`
guards the dict, and a per-key lock single-flights concurrent loads of the same
key so a stampede collapses to one fetch. Every instance self-registers so
`stats()` / `format_stats()` can report total memory held across caches.
"""
import sys
import threading
import time

_registry: list = []
_registry_lock = threading.Lock()


class TTLCache:
    """A named, thread-safe {key: value} cache whose entries expire after `ttl` seconds."""

    def __init__(self, name, ttl):
        self.name = name
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict = {}       # key -> (timestamp, value)
        self._key_locks: dict = {}  # key -> Lock, single-flights that key's loader
        with _registry_lock:
            _registry.append(self)

    # ── reads ───────────────────────────────────────────────────────────────
    def entry(self, key):
        """(timestamp, value) for `key` ignoring the TTL, or None.

        Stale entries are still returned: several callers serve a known-stale
        value as last-known-good rather than failing (warrant results survive a
        CMoney outage this way), and the freshness reporters need the timestamp
        of whatever is actually being served.
        """
        with self._lock:
            return self._data.get(key)

    def fresh(self, key):
        """(timestamp, value) for `key` only while unexpired, else None."""
        with self._lock:
            return self._fresh_locked(key)

    def items(self, include_expired=True):
        """Snapshot list of (key, timestamp, value). Copy: safe to iterate."""
        now = time.time()
        with self._lock:
            return [
                (k, ts, v) for k, (ts, v) in self._data.items()
                if include_expired or now - ts < self.ttl
            ]

    def __len__(self):
        with self._lock:
            return len(self._data)

    # ── writes ──────────────────────────────────────────────────────────────
    def set(self, key, value, ts=None):
        """Store `value` under `key`. `ts` overrides the stamp so a batch of
        keys written together can share one timestamp (callers take the min of
        those stamps as the batch's as-of time)."""
        stamp = time.time() if ts is None else ts
        with self._lock:
            self._data[key] = (stamp, value)
        return stamp

    def get_or_set(self, key, fn, force=False):
        """Cached value for `key`, else `fn()` stored and returned.

        Concurrent misses on the same key run `fn` once and share the result;
        misses on different keys proceed in parallel. `force=True` refetches
        even on a live entry (the manual-refresh path).
        """
        if not force:
            hit = self.fresh(key)
            if hit is not None:
                return hit[1]
        with self._key_lock(key):
            if not force:
                # Another thread may have loaded this key while we queued.
                hit = self.fresh(key)
                if hit is not None:
                    return hit[1]
            value = fn()
            self.set(key, value)
            return value

    def invalidate(self, key=None):
        """Drop one key, or every key when `key` is None. Returns entries dropped."""
        with self._lock:
            if key is None:
                dropped = len(self._data)
                self._data.clear()
                self._key_locks.clear()
                return dropped
            return 1 if self._data.pop(key, None) is not None else 0

    # ── stats ───────────────────────────────────────────────────────────────
    def stats(self):
        """{name, ttl, entries, bytes} — `bytes` is approximate (see _approx_size)."""
        with self._lock:
            entries = list(self._data.values())
        return {
            "name": self.name,
            "ttl": self.ttl,
            "entries": len(entries),
            "bytes": sum(_approx_size(v) for _ts, v in entries),
        }

    # ── internals ───────────────────────────────────────────────────────────
    def _fresh_locked(self, key):
        hit = self._data.get(key)
        if hit is None or time.time() - hit[0] >= self.ttl:
            return None
        return hit

    def _key_lock(self, key):
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock


def _approx_size(value):
    """Rough footprint of one cached value, in bytes.

    Deliberately approximate: this exists to answer "which cache is holding the
    500 MB", not to be exact. sys.getsizeof reports only the container for
    DataFrames and collections, so unwrap the two shapes actually stored here —
    pandas objects (deep memory_usage) and dict/list containers (sum of the
    members, one level down). Never raises: a stats fault must not break a job.
    """
    try:
        usage = getattr(value, "memory_usage", None)
        if usage is not None:
            total = usage(deep=True)
            return int(getattr(total, "sum", lambda: total)())
        if isinstance(value, dict):
            return sys.getsizeof(value) + sum(
                sys.getsizeof(k) + _approx_size(v) for k, v in value.items()
            )
        if isinstance(value, (list, tuple, set)):
            return sys.getsizeof(value) + sum(_approx_size(v) for v in value)
        return sys.getsizeof(value)
    except Exception:
        return 0


def caches():
    """Every live TTLCache, in creation order."""
    with _registry_lock:
        return list(_registry)


def stats():
    """Per-cache stats dicts for every registered cache."""
    return [c.stats() for c in caches()]


def format_stats():
    """One-line aggregate summary for the log stream."""
    rows = stats()
    total = sum(r["bytes"] for r in rows)
    parts = " | ".join(
        f"{r['name']} {r['entries']}/{_mb(r['bytes'])}MB" for r in rows
    )
    return f"total={_mb(total)}MB over {len(rows)} caches | {parts}"


def _mb(n):
    return round(n / (1024 * 1024), 1)


def reset_registry():
    """Forget every registered cache. Tests only — nothing in the app drops caches."""
    with _registry_lock:
        _registry.clear()
