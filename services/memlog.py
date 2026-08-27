"""Memory/duration probes for the memory-constrained (512 MB) host.

Every helper swallows its own errors: a measurement fault must never break the
work being measured.
"""
import os
import threading
import time
from contextlib import contextmanager

import psutil

# cgroup v2 current usage — this, not RSS, is what the host's OOM killer acts
# on (it counts page cache and every process in the container). Absent locally.
_CGROUP_CURRENT = "/sys/fs/cgroup/memory.current"
# The container's actual memory ceiling — comparing _CGROUP_CURRENT against
# this is what turns "usage is going up" into "usage is N% of the limit,"
# which is the number that actually predicts an OOM kill. Absent locally.
_CGROUP_MAX = "/sys/fs/cgroup/memory.max"
# Cumulative CPU-throttle counters: the kernel increments these whenever a
# cgroup with a CPU quota (e.g. Render's "1 vCPU" plan) runs out of its quota
# for the current period and gets paused until the next one. This is the
# actual "hit the CPU limit" signal — CPU% alone can't distinguish "busy" from
# "throttled," but a nonzero nr_throttled delta can only mean the latter.
_CGROUP_CPU_STAT = "/sys/fs/cgroup/cpu.stat"
_CGROUP_CPU_MAX = "/sys/fs/cgroup/cpu.max"

_SAMPLE_INTERVAL = 0.25


def _rss_bytes():
    """RSS of this process plus any live children."""
    try:
        me = psutil.Process(os.getpid())
        total = me.memory_info().rss
        for child in me.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except Exception:
        return 0


def _cgroup_bytes():
    try:
        with open(_CGROUP_CURRENT) as f:
            return int(f.read().strip())
    except Exception:
        return None


def cgroup_memory_limit_bytes():
    """The container's memory ceiling, or None if unavailable/unlimited
    ("max") — e.g. running locally with no cgroup, or a plan with no memory
    cap set."""
    try:
        with open(_CGROUP_MAX) as f:
            raw = f.read().strip()
        return None if raw == "max" else int(raw)
    except Exception:
        return None


def memory_usage_fraction():
    """Current cgroup memory usage as a fraction of the container's limit
    (0-1+), or None if either figure is unavailable. This is the number that
    actually predicts an OOM kill — RSS alone doesn't account for page cache
    or sibling processes the way the cgroup's own accounting does."""
    limit = cgroup_memory_limit_bytes()
    current = _cgroup_bytes()
    if limit is None or current is None or limit <= 0:
        return None
    return current / limit


def cpu_quota_vcpus():
    """Configured CPU quota in vCPU-equivalents (e.g. 1.0 for a "1 vCPU"
    plan), or None if unlimited/unavailable."""
    try:
        with open(_CGROUP_CPU_MAX) as f:
            raw = f.read().strip()
        quota_str, period_str = raw.split()
        return None if quota_str == "max" else int(quota_str) / int(period_str)
    except Exception:
        return None


def cpu_throttle_stats():
    """(nr_throttled, throttled_usec): cumulative counters from cgroup v2's
    cpu.stat — how many times, and for how long in total, the kernel has
    paused this container for exceeding its CPU quota. (None, None) if
    unavailable (no cgroup, or no CPU quota configured to be throttled
    against in the first place). These only ever increase — a caller wanting
    "did throttling happen since I last checked" needs to diff two readings.
    """
    try:
        stats = {}
        with open(_CGROUP_CPU_STAT) as f:
            for line in f:
                key, _, value = line.strip().partition(" ")
                stats[key] = int(value)
        return stats.get("nr_throttled"), stats.get("throttled_usec")
    except Exception:
        return None, None


def _mb(n):
    return round(n / (1024 * 1024), 1)


def _fmt_cg(n):
    return "-" if n is None else _mb(n)


@contextmanager
def measure(name):
    t0 = time.time()
    rss_before = _rss_bytes()
    peak = {"rss": rss_before, "cg": _cgroup_bytes() or 0}
    stop = threading.Event()

    def sample():
        while not stop.wait(_SAMPLE_INTERVAL):
            try:
                peak["rss"] = max(peak["rss"], _rss_bytes())
                cg = _cgroup_bytes()
                if cg is not None:
                    peak["cg"] = max(peak["cg"], cg)
            except Exception:
                return

    sampler = None
    try:
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            stop.set()
            if sampler is not None:
                sampler.join(timeout=1)
            rss_after = _rss_bytes()
            peak["rss"] = max(peak["rss"], rss_after)
            cg_now = _cgroup_bytes()
            if cg_now is not None:
                peak["cg"] = max(peak["cg"], cg_now)
            print(
                f"MEM: task={name} dur={time.time() - t0:.1f}s "
                f"rss_before={_mb(rss_before)}MB rss_after={_mb(rss_after)}MB "
                f"rss_peak={_mb(peak['rss'])}MB "
                f"cg_peak={_fmt_cg(peak['cg'] if cg_now is not None else None)}MB",
                flush=True,
            )
        except Exception:
            pass


def log_baseline(tag):
    try:
        print(
            f"MEM: baseline tag={tag} rss={_mb(_rss_bytes())}MB "
            f"cg={_fmt_cg(_cgroup_bytes())}MB",
            flush=True,
        )
    except Exception:
        pass
