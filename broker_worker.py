"""Broker worker entry point: python -u broker_worker.py (see Dockerfile.worker).

STUB — Phase 0 (issue #20) is infra only. This file exists to prove the
Singapore-region Render Background Worker builds, boots, and stays up; it does
no market work at all.

Deliberately NOT here, and deliberately not imported from the web app:
  - issue #21  broker credential storage / encryption
  - issue #22  KGI + Fubon client integration (login, subscribe, tick handling)
  - issue #23  the real worker scheduler: market-hours gating consulted from the
               web app (docs/adr/0009), the worker_status table and its
               connected / stopped / reconnecting / disconnected states
               (docs/adr/0010), the real heartbeat write, and arb detection

This process is fully separate from wsgi.py / app.py — separate service,
separate region, no shared runtime. It must never be wired into the web app.

The heartbeat below is a log line, not the real mechanism. The persisted
heartbeat timestamp (second line of liveness defence behind Render's own crash
detection, per docs/adr/0010) is written on every scheduler tick and belongs to
#23, which owns the worker_status schema. No Supabase table is invented here.
"""
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone

# Seconds between heartbeat log lines. The real scheduler cadence is #23's
# decision; this is just "often enough to see the process is alive in logs".
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("WORKER_HEARTBEAT_SEC", "60"))

logging.basicConfig(
    level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("broker_worker")

# Set by SIGTERM/SIGINT so the loop exits cleanly. Render sends SIGTERM on
# deploy/restart; once real broker sessions exist (#22) this is where the
# orderly logout goes.
_shutdown = threading.Event()


def _handle_signal(signum, _frame):
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "broker worker starting (stub, issue #20) pid=%s interval=%ss",
        os.getpid(),
        HEARTBEAT_INTERVAL_SEC,
    )

    ticks = 0
    while not _shutdown.is_set():
        ticks += 1
        # TODO(#23): replace with a real scheduler tick — market-hours check,
        # desired-state poll, session reconcile, persisted heartbeat write.
        log.info(
            "heartbeat tick=%s at=%s",
            ticks,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        _shutdown.wait(HEARTBEAT_INTERVAL_SEC)

    log.info("broker worker stopped after %s ticks", ticks)


if __name__ == "__main__":
    main()
