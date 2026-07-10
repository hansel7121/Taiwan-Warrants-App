"""Production entry point: gunicorn -w 1 --threads 8 wsgi:app

Must run with exactly 1 worker — market-data caches (and, in Phase 2, the
APScheduler jobs) live in process memory and would diverge across workers.
"""
import warrant_logic
from app import app

# gunicorn never executes app.py's __main__ block, so kick off the CMoney
# key bootstrap here. Runs in a daemon thread; binding the port is not
# delayed by the 10-30s headless-Chromium scrape.
warrant_logic.prefetch_cmoney_key()
