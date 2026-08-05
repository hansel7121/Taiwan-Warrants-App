"""Broker worker entry point: python -u broker_worker.py (see Dockerfile.worker).

A separate deployed service from the Flask app — separate process, separate
region, no shared runtime (docs/adr/0001). It never imports app.py or wsgi.py,
and it never touches services/scheduler.py's scheduler object or the market-data
fetchers that scheduler drives: exactly one process in this repo scrapes market
data, and it is not this one.

Scope (issue #43): prove the credential path end to end — a credential typed
into the Live warrant form (#42), Fernet-encrypted into Supabase, is read back
here and actually logs in at the broker. That is the entire job. There is no
watchlist, no tick subscription and no arb pipeline yet (#44/#45/#47 own those,
per docs/adr/0001-live-warrant-reuses-shared-watchlist-and-pool.md), so this
worker deliberately never calls `subscribe` — a successful `login()` is the
whole proof surface, and plumbing ticks nothing reads would be scaffolding to
delete.

What runs here, on its own BackgroundScheduler:

  desired_state    poll broker_desired_state and make the set of open broker
                   sessions match what users asked for (#43)
  heartbeat        log that the process is alive and how many sessions it holds

Reconciliation is per Broker Account and failure-isolated: one account's login
blowing up writes that account 'disconnected' and moves on, because both users
share this one process and neither should be able to take the other offline.
"""
import logging
import os
import signal
import sys
import threading

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

# Imported for its .env loading as much as its API: the worker needs the
# Supabase service-role key, and db.py loads the repo-root .env at import.
from services import db  # noqa: F401
from services.broker import desired_state
from services.broker.base import BrokerConnectionError


DESIRED_POLL_SEC = int(os.environ.get("WORKER_DESIRED_POLL_SEC", "20"))
HEARTBEAT_SEC = int(os.environ.get("WORKER_HEARTBEAT_SEC", "60"))

logging.basicConfig(
    level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("broker_worker")

_shutdown = threading.Event()


def client_class(broker):
    """The BrokerClient subclass for `broker`, imported on first use.

    Deliberately not a module-level import: kgi_client pulls in `kgisuperpy` and
    fubon_client pulls in `fubon_neo`, both of which exist only in the worker
    image (Dockerfile.worker). Importing them at module scope would make this
    file unimportable anywhere else — including in tests on a dev machine, where
    neither SDK is installed.
    """
    if broker == "kgi":
        from services.broker.kgi_client import KGIClient
        return KGIClient
    if broker == "fubon":
        from services.broker.fubon_client import FubonClient
        return FubonClient
    raise ValueError(f"unknown broker: {broker!r}")


class Worker:
    """Holds the live broker sessions and keeps them matching desired state."""

    def __init__(self):
        self._lock = threading.Lock()
        # (user_id, broker) -> logged-in BrokerClient. Membership IS the
        # worker's own view of what is connected; worker_status is the copy the
        # UI reads, written on every transition.
        self._clients = {}

    def poll_desired_state(self):
        self.reconcile(desired_state.list_all_desired_states())

    def reconcile(self, rows):
        """Open what users asked for, close everything else.

        A row asking to disconnect and a row that vanished are the same
        instruction — neither is in the wanted set — so both are handled by the
        one difference below rather than by reading `desired_state` twice.

        Closes run before opens so a broker that refuses a second concurrent
        session for one account sees the old one gone first.
        """
        wanted = {
            (r["user_id"], r["broker"])
            for r in rows
            if r.get("desired_state") == "connect"
            and r.get("broker") in desired_state.BROKERS
        }
        with self._lock:
            held = set(self._clients)

        for account in sorted(held - wanted):
            self.close(account)
        for account in sorted(wanted - held):
            self.open(account)

    def open(self, account):
        """Log this Broker Account in.

        Never raises: a broken credential is a state to report, not a reason to
        stop reconciling every other account in the same poll.
        """
        user_id, broker = account
        try:
            client = client_class(broker).from_stored(user_id)
            client.login()
        except BrokerConnectionError as e:
            log.error("login rejected for %s (%s): %s", broker, _short(user_id), e)
            self.set_status(user_id, broker, "disconnected")
            return
        except Exception as e:
            log.error("login failed for %s (%s): %s: %s", broker, _short(user_id),
                      type(e).__name__, e, exc_info=True)
            self.set_status(user_id, broker, "disconnected")
            return

        with self._lock:
            self._clients[account] = client
        log.info("connected: %s (%s)", broker, _short(user_id))
        self.set_status(user_id, broker, "connected")

    def close(self, account):
        """Log this Broker Account out and report 'stopped'.

        'stopped' rather than 'disconnected': the session ended because someone
        asked it to, which is the state the UI must not alarm on.
        """
        user_id, broker = account
        with self._lock:
            client = self._clients.pop(account, None)
        if client is not None:
            try:
                client.logout()
            except Exception as e:
                # The session is dropped from the map either way: a broker that
                # errors on logout must not strand the account as 'connected'.
                log.warning("logout failed for %s (%s): %s: %s", broker,
                            _short(user_id), type(e).__name__, e)
        log.info("stopped: %s (%s)", broker, _short(user_id))
        self.set_status(user_id, broker, "stopped")

    def set_status(self, user_id, broker, status):
        """Publish a transition, swallowing write failures.

        Supabase being briefly unreachable makes the UI stale; letting it raise
        here would make the worker drop live sessions over a status write.
        """
        try:
            desired_state.set_worker_status(user_id, broker, status)
        except Exception as e:
            log.error("worker_status write failed for %s (%s) -> %s: %s: %s",
                      broker, _short(user_id), status, type(e).__name__, e)

    def heartbeat(self):
        """Proof of life in Render's log stream, and what the process holds."""
        with self._lock:
            accounts = sorted(self._clients)
        detail = ": " + ", ".join(f"{b}/{_short(u)}" for u, b in accounts) if accounts else ""
        log.info("alive: %s account(s) connected%s", len(accounts), detail)

    def shutdown(self):
        """Orderly logout of everything held. A deploy or restart is expected,
        so every account is left 'stopped' — the state that neither alarms nor
        reads as a broker outage."""
        with self._lock:
            accounts = sorted(self._clients)
        for account in accounts:
            self.close(account)


def _short(user_id):
    """First segment of a user's UUID — enough to tell two accounts apart in a
    log line without printing a whole user identifier into Render's logs."""
    return str(user_id).split("-")[0]


def _job(name, fn):
    """Run fn, logging failures. A failed job must never stop the scheduler."""
    def run():
        try:
            fn()
        except Exception as e:
            log.error("job %s failed: %s: %s", name, type(e).__name__, e, exc_info=True)
    return run


def build_scheduler(worker):
    """The worker's OWN scheduler — never services/scheduler.py's instance.

    The desired-state poll gets its own single-worker executor because a login
    can block for seconds against a slow broker; sharing the default executor
    would stall the heartbeat and make a busy worker look dead.
    """
    sched = BackgroundScheduler(
        daemon=True,
        executors={
            "default": ThreadPoolExecutor(max_workers=1),
            "session": ThreadPoolExecutor(max_workers=1),
        },
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
    )
    sched.add_job(_job("desired_state", worker.poll_desired_state),
                  "interval", seconds=DESIRED_POLL_SEC, executor="session")
    sched.add_job(_job("heartbeat", worker.heartbeat),
                  "interval", seconds=HEARTBEAT_SEC)
    return sched


def _handle_signal(signum, _frame):
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("broker worker starting pid=%s poll=%ss", os.getpid(), DESIRED_POLL_SEC)
    worker = Worker()

    sched = build_scheduler(worker)
    sched.start()
    log.info("scheduler started")

    _shutdown.wait()

    sched.shutdown(wait=False)
    worker.shutdown()
    log.info("broker worker stopped")


if __name__ == "__main__":
    main()
