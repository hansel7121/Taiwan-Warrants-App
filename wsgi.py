"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import os

from services import memlog
from services import scheduler
from services.broker import live_price
from app import app

memlog.log_baseline("boot")

# gunicorn never executes app.py's __main__ block, so the background refresh
# scheduler would normally start here. It is now OPT-IN via ENABLE_SCHEDULER
# (default OFF) to keep the process off the memory-hungry 15-min refresh jobs
# on a 512 MB host: with it off the site fetches data only on demand, when a
# user presses a button (fetch-on-miss caches + a lazy CMoney-key trigger).
# Everything runs in daemon threads; binding the port is not delayed.
if os.environ.get("ENABLE_SCHEDULER") == "1":
    scheduler.start()
else:
    print("SCHED: disabled (set ENABLE_SCHEDULER=1 to enable)", flush=True)

# The Live-warrant price poller is NOT gated the way the scheduler is.
# ENABLE_SCHEDULER exists to keep the memory-hungry pandas refresh jobs off a
# 512 MB host; this thread does one `select *` on live_prices — a handful of
# rows, bounded by Watchlist capacity — and holds only the latest tick per code.
# The Live-warrant tab shows nothing at all without it, so it has to be up
# whenever the web process is rather than opt-in. Daemon thread; the port is
# bound as usual. See services/broker/live_price.py.
live_price.start()
