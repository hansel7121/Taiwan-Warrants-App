"""Broker worker entry point: python -u broker_worker.py (see Dockerfile.worker).

A separate deployed service from the Flask app — separate process, separate
region, no shared runtime (docs/adr/0001, 0010). It never imports app.py or
wsgi.py, and it never touches services/scheduler.py's scheduler object or the
CMoney/TAIFEX/yfinance fetchers that scheduler drives: exactly one process in
this repo scrapes market data, and it is not this one. What the worker does
import from services/ is infrastructure — Supabase access and the broker
clients — which is the whole reason those live outside logic/.

What runs here, on its own BackgroundScheduler:

  session_gate     poll the web app's /market_hours and open/close the day's
                   broker sessions on the same boundaries the app already uses
                   (docs/adr/0009 — consulted over HTTP, never reimplemented)
  desired_state    poll broker_desired_state for what users asked for (#31)
  mirrors          rebuild the option-side chain and the warrant snapshot from
                   the md_* batch tables the web app writes (docs/adr/0003)
  health           notice a dropped broker connection and drive the reconnect
  retention        age out arb_signals

and, off the scheduler, the live pipeline: a Tick from the ConnectionPool is
merged onto its last known warrant row, checked against the option mirror by the
registered arb strategies, and any match is appended to arb_signals. Ticks
themselves are never persisted or exposed — they live in a bounded in-memory
queue and are discarded once checked (docs/adr/0003).
"""
import logging
import os
import queue
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

import requests
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from logic import arb_logic
# Imported for its .env loading as much as its API: the worker needs the
# Supabase service-role key, and db.py loads the repo-root .env at import.
from services import db  # noqa: F401
from services import db_signals
from services.broker import alerts
from services.broker import desired_state
from services.broker import mirror
from services.broker import pool as broker_pool
from services.broker import resilience
from services.broker.tick_translate import TickTranslationError, tick_to_warrant_row


# Base URL of the web app. Local default so a dev run needs no env var; on the
# deployed worker this is the Render web service's URL.
WEB_APP_URL = os.environ.get("WEB_APP_URL", "http://127.0.0.1:5001").rstrip("/")

# The warrant leg only trades 09:00-13:30 TPE, a narrower window than TAIFEX's,
# so the executability constraint is the equity session — the same gate
# scheduler.py puts on its suggest job, for the same reason.
GATE_MARKET = "tw_equity"

SESSION_GATE_SEC = 60
DESIRED_POLL_SEC = 20
HEALTH_CHECK_SEC = 30
HEARTBEAT_SEC = int(os.environ.get("WORKER_HEARTBEAT_SEC", "60"))
SIGNAL_RETENTION_HOURS = 48

# Bounded on purpose: if checking falls behind the feed, dropping the oldest
# prints is correct. A stale tick produces a signal about a price that no longer
# exists, and an unbounded queue turns a slow Supabase write into an OOM.
TICK_QUEUE_MAX = 2000

logging.basicConfig(
    level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("broker_worker")

_shutdown = threading.Event()


def market_open(market=GATE_MARKET):
    """Ask the web app whether `market` is trading (docs/adr/0009).

    Fails CLOSED: an unreachable web app means the worker does not open broker
    sessions it cannot justify. The opposite default would have it logging in on
    a holiday every time the app restarts.
    """
    try:
        r = requests.get(f"{WEB_APP_URL}/market_hours",
                         params={"market": market}, timeout=10)
        r.raise_for_status()
        return bool(r.json().get("open"))
    except Exception as e:
        log.warning("market-hours check failed (%s: %s); treating %s as closed",
                    type(e).__name__, e, market)
        return False


class Worker:
    """Owns the mirrors, the tick pipeline, and the supervised connection pool."""

    def __init__(self):
        self._lock = threading.Lock()
        self._option_mirror = arb_logic.OptionMirror()
        self._warrant_rows = {}
        # Broker Accounts the current pool covers, as (user_id, broker) — the
        # rows worker_status transitions are written to.
        self._accounts = []
        self._pool_accounts = []  # BrokerAccounts the next _open_pool() builds from
        self._wanted = set()      # user_ids whose desired_state is 'connect'
        self._market_open = False
        self._connected_users = []
        self._ticks = queue.Queue(maxsize=TICK_QUEUE_MAX)
        self._dropped = 0
        self.supervisor = resilience.SessionSupervisor(
            open_pool=self._open_pool,
            on_status=self._write_status,
            alert=self._alert,
        )

    # -- mirrors ------------------------------------------------------------
    def refresh_mirrors(self):
        option_mirror, opt_as_of = mirror.load_option_mirror()
        warrant_rows, w_as_of = mirror.load_warrant_rows()
        with self._lock:
            self._option_mirror = option_mirror
            # Only swap in a non-empty warrant snapshot: a transiently empty read
            # would otherwise blind every tick (no snapshot row -> no signal)
            # until the next refresh, and a slightly stale row still prices
            # correctly since a tick overwrites the price anyway.
            if warrant_rows:
                self._warrant_rows = warrant_rows
        log.info("mirrors refreshed: warrants=%s (as_of=%s) options as_of=%s",
                 len(warrant_rows), w_as_of, opt_as_of)

    # -- desired state / session gate ---------------------------------------
    def poll_desired_state(self):
        rows = desired_state.list_all_desired_states()
        wanted = {r["user_id"] for r in rows if r.get("desired_state") == "connect"}
        with self._lock:
            changed = wanted != self._wanted
            self._wanted = wanted
        if changed:
            log.info("desired state changed: %s user(s) want connect", len(wanted))
        self.reconcile()

    def poll_session_gate(self):
        is_open = market_open()
        with self._lock:
            changed = is_open != self._market_open
            self._market_open = is_open
        if changed:
            log.info("%s is now %s", GATE_MARKET, "open" if is_open else "closed")
        self.reconcile()

    def reconcile(self):
        """Make the live pool match (what users asked for) AND (market open).

        Both inputs gate the same thing, so they converge here rather than each
        driving the pool directly — otherwise a desired-state poll could reopen
        sessions the end-of-day close just shut.
        """
        with self._lock:
            target = sorted(self._wanted) if self._market_open else []
            current = self._connected_users

        if target == current and self.supervisor.running == bool(target):
            return

        if self.supervisor.running:
            # Expected close: 'stopped', never 'reconnecting'. Covers both the
            # end-of-day gate and a user clicking Disconnect.
            self.supervisor.stop()

        with self._lock:
            self._connected_users = target
            self._accounts = []

        if not target:
            log.info("no live sessions (market_open=%s, wanted=%s)",
                     self._market_open, len(self._wanted))
            return

        accounts = broker_pool.accounts_for(target)
        if not accounts:
            log.warning("users want connect but have no stored broker accounts")
            with self._lock:
                self._connected_users = []
            return
        with self._lock:
            self._accounts = [(a.user_id, a.broker) for a in accounts]
            self._pool_accounts = accounts
        self.supervisor.start()

    def _open_pool(self):
        with self._lock:
            accounts = list(self._pool_accounts)
            watchlist = sorted(self._warrant_rows)
        if not accounts:
            raise RuntimeError("no broker accounts to open")
        pool = broker_pool.ConnectionPool(accounts, watchlist, self._on_tick)
        pool.open()
        if pool.unassigned:
            # Over-subscription is a real state, not an error: those codes go
            # dark until capacity grows (docs/adr/0004).
            log.warning("watchlist exceeds pool capacity: %s codes unsubscribed",
                        len(pool.unassigned))
        log.info("pool open: %s connection(s), %s codes subscribed",
                 len(pool.clients), len(watchlist) - len(pool.unassigned))
        return pool

    def _write_status(self, status):
        with self._lock:
            accounts = list(self._accounts)
        for user_id, broker in accounts:
            desired_state.set_worker_status(user_id, broker, status)
        log.info("worker_status -> %s for %s account(s)", status, len(accounts))

    def _alert(self, reason):
        with self._lock:
            accounts = list(self._accounts)
        brokers = ", ".join(sorted({b for _u, b in accounts})) or "unknown"
        text = (f"Broker feed reconnecting for {brokers}: {reason}. "
                f"Live arb scanning is offline until it recovers.")
        log.error("ALERT: %s", text)
        alerts.notify(text)

    def check_health(self):
        self.supervisor.check_health()

    # -- tick pipeline ------------------------------------------------------
    def _on_tick(self, tick):
        """Called on the broker SDK's own callback thread — enqueue only.

        Matching and the Supabase write happen on the consumer thread below, so
        a slow database call can never stall the feed.
        """
        try:
            self._ticks.put_nowait(tick)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("tick queue full, dropped %s tick(s)", self._dropped)

    def consume_ticks(self):
        while not _shutdown.is_set():
            try:
                tick = self._ticks.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handle_tick(tick)
            except Exception as e:
                # One malformed tick must never kill the consumer; the feed
                # keeps running and the next tick is checked normally.
                log.warning("tick handling failed for %s: %s: %s",
                            getattr(tick, "code", "?"), type(e).__name__, e)

    def handle_tick(self, tick):
        """Merge, check, and append. Returns the number of signals written."""
        with self._lock:
            snapshot_row = self._warrant_rows.get(str(tick.code))
            option_mirror = self._option_mirror
        try:
            row = tick_to_warrant_row(tick, snapshot_row)
        except TickTranslationError as e:
            log.debug("tick not translatable: %s", e)
            return 0

        matches = arb_logic.check_tick(row, option_mirror)
        if not matches:
            return 0
        signals = [
            db_signals.to_signal_row(m, tick_ts=tick.ts, tick_broker=tick.broker)
            for m in matches
        ]
        db_signals.insert_signals(signals)
        log.info("%s signal(s) from %s tick on %s", len(signals), tick.broker, tick.code)
        return len(signals)

    # -- housekeeping -------------------------------------------------------
    def prune_signals(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=SIGNAL_RETENTION_HOURS)
        db_signals.delete_signals_before(cutoff.isoformat())

    def heartbeat(self):
        log.info("alive: pool=%s accounts=%s queued=%s warrants=%s",
                 "up" if self.supervisor.running else "down",
                 len(self._accounts), self._ticks.qsize(), len(self._warrant_rows))

    def shutdown(self):
        """Orderly logout. A deploy or restart is expected, so it writes
        'stopped' — the same state an end-of-day close writes, and one that
        neither alerts nor shows the offline banner."""
        if self.supervisor.running:
            self.supervisor.stop()


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

    Same single-worker default executor as the web app's, for the same reason
    (no two data jobs overlap on a small box). Sessions get their own
    single-worker executor because a reconnect walks a backoff ladder that can
    block for minutes; on the shared executor that would silently stall the
    mirror refresh and the desired-state poll for the whole outage.
    """
    sched = BackgroundScheduler(
        daemon=True,
        executors={
            "default": ThreadPoolExecutor(max_workers=1),
            "session": ThreadPoolExecutor(max_workers=1),
        },
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
    )
    # Mirrors run a few minutes behind the web app's own 0/15/30/45 sync grid so
    # they read a batch that has actually been written, not one mid-flight.
    sched.add_job(_job("mirrors", worker.refresh_mirrors),
                  CronTrigger(minute="4,19,34,49"))
    sched.add_job(_job("session_gate", worker.poll_session_gate),
                  "interval", seconds=SESSION_GATE_SEC, executor="session")
    sched.add_job(_job("desired_state", worker.poll_desired_state),
                  "interval", seconds=DESIRED_POLL_SEC, executor="session")
    sched.add_job(_job("health", worker.check_health),
                  "interval", seconds=HEALTH_CHECK_SEC, executor="session")
    sched.add_job(_job("retention", worker.prune_signals),
                  CronTrigger(hour=6, minute=0))
    sched.add_job(_job("heartbeat", worker.heartbeat),
                  "interval", seconds=HEARTBEAT_SEC)
    return sched


def _handle_signal(signum, _frame):
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("broker worker starting pid=%s web_app=%s", os.getpid(), WEB_APP_URL)
    worker = Worker()

    # Mirrors before the scheduler: the watchlist a pool subscribes to comes
    # from the warrant snapshot, so an empty first refresh would open sessions
    # with nothing on them.
    try:
        worker.refresh_mirrors()
    except Exception as e:
        log.error("initial mirror load failed: %s: %s", type(e).__name__, e)

    consumer = threading.Thread(target=worker.consume_ticks,
                                name="tick-consumer", daemon=True)
    consumer.start()

    sched = build_scheduler(worker)
    sched.start()
    log.info("scheduler started")

    _shutdown.wait()

    sched.shutdown(wait=False)
    worker.shutdown()
    consumer.join(timeout=5)
    log.info("broker worker stopped")


if __name__ == "__main__":
    main()
