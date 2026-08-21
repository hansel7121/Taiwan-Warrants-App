"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import os
import signal

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

# A Coolify redeploy sends the gunicorn worker SIGTERM; without this the
# Fubon session (if any) is abandoned mid-connection instead of logging out
# (issue #88). Chain to gunicorn's own handler so its graceful shutdown
# still runs afterward.
_prev_sigterm_handler = signal.getsignal(signal.SIGTERM)


def _graceful_shutdown(signum, frame):
    print("WSGI: SIGTERM received, closing Fubon session", flush=True)
    try:
        live_warrant.stop_session()
    except Exception as e:
        print(f"WSGI: session teardown on shutdown failed: {e}", flush=True)
    if callable(_prev_sigterm_handler):
        _prev_sigterm_handler(signum, frame)


signal.signal(signal.SIGTERM, _graceful_shutdown)
