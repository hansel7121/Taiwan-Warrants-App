"""Reconnect-with-backoff around a ConnectionPool, plus the status it reports.

pool.py deliberately has no resilience (a broker failure propagates untouched);
this is the layer that catches it. It owns three things docs/adr/0004 specifies:

  - the backoff ladder (5s doubling to a 60s cap, 10 attempts, then terminal)
  - the four-state worker_status transitions, including the intermediate ones
  - the alert gate: once per incident, only past a grace threshold, never for a
    stop

Time is injected (`sleep`/`clock`) so the whole ladder is testable without
actually waiting several minutes, and the pool is built by a caller-supplied
factory so the state machine never needs a broker SDK.
"""
import logging


BACKOFF_INITIAL_SEC = 5.0
BACKOFF_CAP_SEC = 60.0
MAX_RECONNECT_ATTEMPTS = 10
# Alert only once a drop has outlived a normal reconnect cycle. The first
# retry lands at 5s, so 15s means a blip that recovers on attempt one or two
# never pages anyone — the alert-fatigue failure mode docs/adr/0004 calls out,
# where a person ignores both redundant channels.
ALERT_AFTER_SEC = 15.0

log = logging.getLogger("broker_worker.resilience")


def backoff_delays(attempts=MAX_RECONNECT_ATTEMPTS,
                   initial=BACKOFF_INITIAL_SEC, cap=BACKOFF_CAP_SEC):
    """Seconds to wait before each reconnect attempt: 5, 10, 20, 40, 60, 60..."""
    delay = initial
    for _ in range(attempts):
        yield delay
        delay = min(delay * 2, cap)


class SessionSupervisor:
    """Keeps one ConnectionPool up, reporting every transition it goes through.

    `open_pool()` returns an already-opened pool (construct + `.open()`); the
    supervisor only ever calls it and `pool.close()`, so it works against the
    real ConnectionPool or a stub. `on_status(status)` receives one of the four
    worker_status values and is responsible for fanning it out to whichever
    Broker Accounts the pool covers.
    """

    def __init__(self, open_pool, on_status, alert=None, sleep=None, clock=None):
        import time as _time

        self._open_pool = open_pool
        self._on_status = on_status
        self._alert = alert
        self._sleep = sleep or _time.sleep
        self._clock = clock or _time.monotonic
        self.pool = None
        # Set while stop() is in flight so an in-progress reconnect abandons the
        # ladder instead of racing the shutdown back to 'connected'.
        self._stopping = False
        self._alerted = False

    @property
    def running(self):
        return self.pool is not None

    def start(self):
        """Open the pool. A failure here enters the same backoff ladder a
        mid-session drop does — an unreachable broker at 09:00 is the same
        outage as one at 11:00, and retrying is better than one shot at open."""
        if self.pool is not None:
            return True
        self._stopping = False
        try:
            self.pool = self._open_pool()
        except Exception as e:
            log.warning("initial pool open failed: %s: %s", type(e).__name__, e)
            return self._reconnect(f"could not open broker connections: {e}")
        self._alerted = False
        self._status("connected")
        return True

    def stop(self):
        """Expected shutdown: end-of-day, or the user asking to disconnect.

        Writes 'stopped' directly and never passes through 'reconnecting', so a
        routine nightly close neither alerts nor shows the offline banner.
        """
        self._stopping = True
        self._close_quietly()
        self.pool = None
        self._alerted = False
        self._status("stopped")

    def check_health(self):
        """Poll the pool's clients; an unexpectedly-down one starts a reconnect.

        The broker SDKs surface a drop as `is_connected` going false rather than
        as a callback, so liveness is polled by the worker's scheduler instead
        of being pushed here.
        """
        if self.pool is None or self._stopping:
            return True
        clients = getattr(self.pool, "clients", []) or []
        if clients and all(_is_connected(c) for c in clients):
            return True
        return self._reconnect("broker connection dropped")

    def _reconnect(self, reason):
        """Walk the backoff ladder. True if recovered, False if exhausted."""
        self._status("reconnecting")
        started = self._clock()

        for attempt, delay in enumerate(backoff_delays(), start=1):
            self._sleep(delay)
            if self._stopping:
                return False
            self._maybe_alert(started, reason)
            self._close_quietly()
            try:
                self.pool = self._open_pool()
            except Exception as e:
                log.warning("reconnect attempt %s failed: %s: %s",
                            attempt, type(e).__name__, e)
                self.pool = None
                continue
            log.info("reconnected on attempt %s", attempt)
            self._alerted = False
            self._status("connected")
            return True

        # Terminal. No second alert: the operator was already paged when this
        # incident crossed the grace threshold, and 'disconnected' is the same
        # incident reaching its end, not a new one.
        self.pool = None
        self._status("disconnected")
        return False

    def _maybe_alert(self, started, reason):
        if self._alerted or self._alert is None:
            return
        if self._clock() - started < ALERT_AFTER_SEC:
            return
        self._alerted = True
        try:
            self._alert(reason)
        except Exception as e:
            log.warning("alert dispatch failed: %s: %s", type(e).__name__, e)

    def _status(self, status):
        try:
            self._on_status(status)
        except Exception as e:
            # A Supabase hiccup while reporting must not abort a reconnect that
            # would otherwise succeed.
            log.warning("status write failed (%s): %s: %s", status, type(e).__name__, e)

    def _close_quietly(self):
        if self.pool is None:
            return
        try:
            self.pool.close()
        except Exception as e:
            log.warning("pool close failed: %s: %s", type(e).__name__, e)


def _is_connected(client):
    try:
        return bool(client.is_connected)
    except Exception:
        return False
