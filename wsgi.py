"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches and the APScheduler
refresh jobs live in process memory and would diverge across workers.
"""
import scheduler
from app import app

# gunicorn never executes app.py's __main__ block, so the background refresh
# scheduler (which also bootstraps the CMoney key) starts here. Everything
# runs in daemon threads; binding the port is not delayed.
scheduler.start()
