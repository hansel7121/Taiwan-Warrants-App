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
