"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import os
import signal
import threading

from services import live_warrant
from services import memlog
from services import scheduler
from app import app

memlog.log_baseline("boot")

# Opt-in via ENABLE_SCHEDULER (default OFF) to keep a 512MB host off the
# memory-hungry refresh jobs; with it off, data fetches only on demand.
if os.environ.get("ENABLE_SCHEDULER") == "1":
    scheduler.start()
else:
    print("SCHED: disabled (set ENABLE_SCHEDULER=1 to enable)", flush=True)

# Logs out of Fubon on redeploy (SIGTERM) instead of abandoning the session; chains to gunicorn's own handler.
_prev_sigterm_handler = signal.getsignal(signal.SIGTERM)
_SHUTDOWN_TEARDOWN_TIMEOUT_S = 8


def _safe_stop_session():
    try:
        live_warrant.stop_session()
    except Exception as e:
        print(f"WSGI: session teardown on shutdown failed: {e}", flush=True)


def _graceful_shutdown(signum, frame):
    print("WSGI: SIGTERM received, closing Fubon session", flush=True)
    t = threading.Thread(target=_safe_stop_session, daemon=True)
    t.start()
    t.join(timeout=_SHUTDOWN_TEARDOWN_TIMEOUT_S)  # bounded so a slow/unreachable broker can't block gunicorn's own shutdown
    if callable(_prev_sigterm_handler):
        _prev_sigterm_handler(signum, frame)


signal.signal(signal.SIGTERM, _graceful_shutdown)
