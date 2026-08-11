"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import os

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
