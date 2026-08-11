"""Broker worker entry point: python -u broker_worker.py (see Dockerfile.worker).

A separate deployed service from the Flask app — separate process, separate
region, no shared runtime (docs/adr/0001). It never imports app.py or wsgi.py,
and it never touches services/scheduler.py's scheduler object or the market-data
fetchers that scheduler drives: exactly one process in this repo scrapes market
data, and it is not this one.

Scope (issue #43): prove the credential path end to end — a credential typed
into the Live warrant form (#42), Fernet-encrypted into Supabase, is read back
here and actually logs in at the broker.

Scope (issue #44): keep those already-open sessions subscribed to the shared
Watchlist. Edits take effect intraday, on connections that are already up, with
no relogin (docs/adr/0002), so the worker reads the Watchlist on a tight
interval while TWSE is open and applies the minimal diff `pool.reassign`
computes.

Scope (issue #46): give the Ticks those subscriptions produce somewhere to go.
This process and the Flask app are separate Render services sharing no memory,
so every tick is upserted into `live_prices` (supabase/migrations/009) — one row
per code, latest price wins. The web process follows that table over Supabase
Realtime and pushes it to browsers as SSE (docs/adr/0003); writing the relay is
where this worker's part ends. See `_relay_tick`.

What runs here, on its own BackgroundScheduler:

  desired_state    poll broker_desired_state and make the set of open broker
                   sessions match what users asked for (#43)
  watchlist        poll the shared Watchlist and subscribe/unsubscribe the open
                   sessions into agreement with it, market-hours only (#44)
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
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

# Imported for its .env loading as much as its API: the worker needs the
# Supabase service-role key, and db.py loads the repo-root .env at import.
from services import db  # noqa: F401
from services.broker import desired_state
from services.broker import live_assignment
from services.broker import pool
from services.broker import watchlist
from services.broker.base import BrokerConnectionError
# One pure weekday/time-of-day predicate, and the only thing this worker takes
# from services/scheduler.py. The rule this module opens with is about that
# scheduler's OBJECT and the fetchers it drives — none of which are touched by
# reading a function that decides whether TWSE is open. Copying the trading
# window here instead would give the repo two definitions of market hours, free
# to drift; docs/adr/0002 asks for the existing gate reused, not reimplemented.
from services.scheduler import is_market_open


DESIRED_POLL_SEC = int(os.environ.get("WORKER_DESIRED_POLL_SEC", "20"))
# Tight enough that a code typed into the Live warrant form starts ticking
# before the user wonders whether it worked (docs/adr/0002), slow enough that
# the poll is a few hundred cheap selects per session rather than a hammering:
# the query is one indexed read of a table that holds tens of rows.
WATCHLIST_POLL_SEC = int(os.environ.get("WORKER_WATCHLIST_POLL_SEC", "5"))
HEARTBEAT_SEC = int(os.environ.get("WORKER_HEARTBEAT_SEC", "60"))
# The worker->web relay for live prices (supabase/migrations/009). Named here
# rather than in a services/ module because this file is its only writer.
LIVE_PRICES_TABLE = "live_prices"
# The worker->web relay for order-book depth (supabase/migrations/012, #51).
# Separate table from live_prices: a depth snapshot is five arrays, not a
# scalar price, and today only KGI produces one.
LIVE_DEPTH_TABLE = "live_depth"
# Append-only tick history, additive to the live_prices cache above
# (supabase/migrations/013, #52).
LIVE_PRICE_TICKS_TABLE = "live_price_ticks"
# How long a tick history row survives before _cleanup_tick_history drops it.
TICK_RETENTION_DAYS = int(os.environ.get("WORKER_TICK_RETENTION_DAYS", "7"))
# Cleanup is a background sweep, not a per-tick cost, so it runs on its own
# slow interval rather than after every relayed tick.
TICK_CLEANUP_SEC = int(os.environ.get("WORKER_TICK_CLEANUP_SEC", str(6 * 3600)))

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
        # What is actually subscribed on which connection — the baseline every
        # Watchlist diff is computed against. Empty at startup because a fresh
        # process holds no sessions and therefore no subscriptions; from then on
        # it only ever moves when this worker's own subscribe/unsubscribe calls
        # move it. Note it is NOT a mirror of `_clients`: which connections
        # exist is the desired-state loop's business, and reaches the Watchlist
        # loop through `accounts` instead.
        self._current = pool.Assignment()
        # Codes the open pool has no room for, remembered so an unchanged
        # overflow is not re-logged every few seconds. Unlike the two above it
        # needs no lock: it is read and written by the Watchlist job alone,
        # which runs on its own single-worker executor.
        self._rejected = set()

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

    def poll_watchlist(self):
        """Fold the shared Watchlist into the open pool, while TWSE is open.

        Gated on tw_equity, the same market the warrant sync grid runs under in
        services/scheduler.py: a warrant is a TWSE-listed instrument and there
        are no ticks to chase outside 09:00-13:30 TPE. The gate is checked here
        rather than expressed in the trigger because a market-hours-aware
        trigger is a lot of machinery to say `return`.
        """
        if not is_market_open("tw_equity"):
            return
        self.apply_watchlist(watchlist.list_codes())

    def apply_watchlist(self, codes):
        """Subscribe and unsubscribe the open connections into agreement with `codes`.

        The pool planned against is exactly the Broker Accounts with an open
        session right now, not every credentialed account. `reassign` places
        codes on connections and this loop can only act on a connection it is
        holding a client for, so handing it an account with spare Capacity Tier
        and no live session would place codes on a connection nobody opened —
        the "sits in the list looking watched and never ticks" state that
        docs/adr/0002 exists to prevent. An account whose session is down is
        therefore absent from the plan entirely, and comes back into it by
        itself on the poll after the desired-state loop reconnects it.
        """
        with self._lock:
            held = set(self._clients)
            current = self._current

        if not held:
            # Nothing is connected, so nothing is subscribed anywhere: the
            # sessions took their subscriptions down with them. Recording that
            # is what stops the next poll from trying to unsubscribe slots on
            # connections that no longer exist.
            self._note_rejected([])
            with self._lock:
                self._current = pool.Assignment()
            self._publish_current()
            return

        diff = pool.reassign(self._open_accounts(held), current, codes)

        # Unsubscribes before subscribes, mirroring reconcile()'s
        # closes-before-opens: a symbol freed on a full connection is what makes
        # room for an add landing on that same connection in this same diff.
        for op in diff.unsubscribes:
            self._run_op("unsubscribe", op,
                         lambda client, op=op: client.unsubscribe(list(op.codes)))

        missed = set()
        for op in diff.subscribes:
            ok = self._run_op(
                "subscribe", op,
                lambda client, op=op: client.subscribe(
                    list(op.codes), _relay_tick, on_depth=_relay_depth))
            if not ok:
                missed.update(op.codes)

        self._note_rejected(diff.rejected)
        with self._lock:
            self._current = _without(diff.assignment, missed)
        self._publish_current()

    def _publish_current(self):
        """Hand the web process what this worker actually holds (docs/adr/0006).

        Swallows write failures for the same reason set_status does: a briefly
        unreachable Supabase must not take the watchlist loop down with it.
        """
        with self._lock:
            current = self._current
        try:
            live_assignment.publish(current)
        except Exception as e:
            log.error("live_assignment write failed: %s: %s", type(e).__name__, e)

    def _open_accounts(self, held):
        """The Broker Accounts this loop can actually place codes on.

        `pool.accounts_for` answers with every stored account of the given
        users, so it is filtered back down to the (user_id, broker) pairs that
        are open — see apply_watchlist for why.

        `connections` is capped at 1 for the same reason one step lower down:
        the worker opens exactly one client per Broker Account (`_clients` is
        keyed by the pair), so connections 2..n of a Capacity Tier are not open
        and cannot carry a subscribe either. KNOWN GAP: that makes the capacity
        this loop can fill narrower than the add-time check in
        services/broker/watchlist.py, which counts the whole tier. The overflow
        that creates surfaces in `rejected` and gets logged — never silently
        dropped — and closes for good when the worker learns to open a client
        per connection.
        """
        user_ids = sorted({user_id for user_id, _ in held})
        return [
            replace(account, connections=1)
            for account in pool.accounts_for(user_ids)
            if (account.user_id, account.broker) in held
        ]

    def _run_op(self, kind, op, action):
        """Apply one SlotOp to its open client, reporting whether it took.

        Never raises, per open()/close(): one broker refusing one subscribe must
        not cost every op queued behind it, because both users' codes ride this
        one loop and neither should be able to blank the other's prices.
        """
        with self._lock:
            client = self._clients.get((op.user_id, op.broker))
        codes = ", ".join(op.codes)
        if client is None:
            # The desired-state loop closed this session between the plan and
            # now; its subscriptions went with it and there is nothing to call.
            log.debug("%s skipped for %s (%s): no open session", kind, op.broker,
                      _short(op.user_id))
            return False
        try:
            action(client)
        except Exception as e:
            log.error("%s failed for %s (%s) on %s: %s: %s", kind, op.broker,
                      _short(op.user_id), codes, type(e).__name__, e)
            self._evict(op.user_id, op.broker, client)
            return False
        log.info("%s %s on %s (%s)", kind, codes, op.broker, _short(op.user_id))
        return True

    def _evict(self, user_id, broker, client):
        """Drop a session whose op just failed, so reconcile() reopens it fresh.

        A failed subscribe/unsubscribe is the only signal this worker can trust:
        the SDK's own disconnect callback does not fire on every drop (seen on a
        dead Fubon socket that kept throwing with no callback ever firing), so
        `is_connected` can stay stuck reporting True forever. Retrying against
        the same broken client is what let a dead session sit in `held`
        indefinitely with prices never landing; dropping it here is what lets
        the next reconcile() see the account as wanted-but-not-held again and
        log it back in.
        """
        with self._lock:
            # Already gone or replaced by another op/poll - nothing to evict.
            if self._clients.get((user_id, broker)) is not client:
                return
            del self._clients[(user_id, broker)]
        try:
            client.logout()
        except Exception as e:
            log.warning("logout failed for %s (%s) during reconnect: %s: %s",
                        broker, _short(user_id), type(e).__name__, e)
        self.set_status(user_id, broker, "reconnecting")

    def _note_rejected(self, rejected):
        """Report codes the open pool has no room for, once per change.

        Normally empty: the add route capacity-checks before a code ever reaches
        the Watchlist table (services/broker/watchlist.py). It fills when
        capacity shrinks under a list that already fitted — a session dropping,
        a Capacity Tier edited down — which is a thing to say out loud, not to
        raise on, and certainly not to repeat every few seconds.
        """
        current = set(rejected)
        if current == self._rejected:
            return
        self._rejected = current
        if current:
            log.warning("watchlist over the open pool's capacity, not subscribed: %s",
                        ", ".join(sorted(current)))
        else:
            log.info("watchlist fits the open pool again")

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


def _relay_tick(tick):
    """Publish one Tick to the live-price relay the web process reads.

    Called on the SDK's own callback thread, once per print, for every code in
    the shared Watchlist.

    Keyed on `code`, so the row for a code is overwritten rather than appended
    to: `live_prices` is a cache of the latest price per watched code, not a
    tick history. The append-only history lives in `live_price_ticks` instead
    (supabase/migrations/013_live_price_ticks.sql, #52), written here right
    after this upsert. `ts` is the tick's own exchange timestamp rather than
    db._now(), because a consumer judging whether a price is stale must measure
    against when the trade printed, not against when this write happened.

    No try/except, per the thin-wrapper convention services/broker/desired_state
    documents: a raise lands in the SDK's callback thread rather than in the
    poll loop, and swallowing it here would silently freeze prices at their last
    good value with nothing in the log to say so.
    """
    log.debug("tick -> %s: %s", LIVE_PRICES_TABLE, tick)
    row = {
        "code": tick.code,
        "price": tick.price,
        # Both clients build this datetime tz-aware (kgi_client and
        # fubon_client's _tick_time), so the string carries an explicit offset
        # and Postgres stores the instant the trade actually printed, whatever
        # timezone the worker container happens to run on.
        "ts": tick.ts.isoformat(),
        "broker": tick.broker,
        "qty": tick.qty,  # None for Fubon today (#54); column is nullable
        "instrument": tick.instrument,  # "warrant" or "tw_option" (#55)
    }
    db._run(
        lambda c: c.table(LIVE_PRICES_TABLE)
        .upsert(row, on_conflict="code")
        .execute()
    )
    # Additive append, not a replacement (#52): live_prices stays the latest-
    # price cache above, this is the growing history the cache can't hold.
    db._run(
        lambda c: c.table(LIVE_PRICE_TICKS_TABLE)
        .insert({"code": tick.code, "broker": tick.broker, "price": tick.price,
                 "qty": tick.qty, "ts": tick.ts.isoformat(),
                 "instrument": tick.instrument})
        .execute()
    )


def _cleanup_tick_history():
    """Drop live_price_ticks rows past TICK_RETENTION_DAYS, so history stays bounded (#52)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TICK_RETENTION_DAYS)).isoformat()
    db._run(
        lambda c: c.table(LIVE_PRICE_TICKS_TABLE)
        .delete()
        .lt("ts", cutoff)
        .execute()
    )


def _relay_depth(depth):
    """Publish one Depth to the live-depth relay, keyed on code (mirrors _relay_tick)."""
    log.debug("depth -> %s: %s", LIVE_DEPTH_TABLE, depth)
    row = {
        "code": depth.code,
        "bid_prices": list(depth.bid_prices),
        "bid_volumes": list(depth.bid_volumes),
        "ask_prices": list(depth.ask_prices),
        "ask_volumes": list(depth.ask_volumes),
        "ts": depth.ts.isoformat(),
        "broker": depth.broker,
        "instrument": depth.instrument,  # "warrant" or "tw_option" (#55)
    }
    db._run(
        lambda c: c.table(LIVE_DEPTH_TABLE)
        .upsert(row, on_conflict="code")
        .execute()
    )


def _without(assignment, codes):
    """`assignment` minus `codes` — the ones whose subscribe did not take.

    Leaving a refused code in the recorded state would have the next diff treat
    it as already streaming and never try again. Dropping it makes that poll see
    it as an add, which is how a failed login gets retried too. Unsubscribes get
    no such treatment: like close()'s logout, a connection that errors on the
    way out is still treated as let go.
    """
    if not codes:
        return assignment
    slots = [replace(slot, codes=tuple(c for c in slot.codes if c not in codes))
             for slot in assignment.slots]
    return pool.Assignment(slots=[slot for slot in slots if slot.codes],
                           unassigned=assignment.unassigned)


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
    would stall the heartbeat and make a busy worker look dead. The Watchlist
    poll gets a third for the same reason and its own: subscribe/unsubscribe are
    SDK websocket calls that can block, and behind a login on the session
    executor a 5-second poll would queue up behind a 20-second one.
    """
    sched = BackgroundScheduler(
        daemon=True,
        executors={
            "default": ThreadPoolExecutor(max_workers=1),
            "session": ThreadPoolExecutor(max_workers=1),
            "watchlist": ThreadPoolExecutor(max_workers=1),
        },
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600},
    )
    sched.add_job(_job("desired_state", worker.poll_desired_state),
                  "interval", seconds=DESIRED_POLL_SEC, executor="session")
    sched.add_job(_job("watchlist", worker.poll_watchlist),
                  "interval", seconds=WATCHLIST_POLL_SEC, executor="watchlist")
    sched.add_job(_job("heartbeat", worker.heartbeat),
                  "interval", seconds=HEARTBEAT_SEC)
    sched.add_job(_job("tick_cleanup", _cleanup_tick_history),
                  "interval", seconds=TICK_CLEANUP_SEC)
    return sched


def _handle_signal(signum, _frame):
    log.info("received signal %s, shutting down", signum)
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("broker worker starting pid=%s poll=%ss watchlist=%ss", os.getpid(),
             DESIRED_POLL_SEC, WATCHLIST_POLL_SEC)
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
