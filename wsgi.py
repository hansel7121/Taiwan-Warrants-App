"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import os

import memlog
import scheduler
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
