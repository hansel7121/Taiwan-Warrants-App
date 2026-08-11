"""Standalone entry point for the live-price SSE service (issue #58 item 1).

Runs sse.py's blueprint alone, on gunicorn's gevent worker class instead of
the main app's gthread pool: /live_prices/stream holds its connection open
for the tab's whole life, so a cooperative-scheduling worker built for many
idle connections fits it far better than pinning an OS thread per open tab.

Deliberately a separate process from the main app (wsgi.py) rather than a
worker-class change there — the main app's other routes are CPU-bound
(Black-Scholes/IV solves, IV surface interpolation, arb matching), and
gevent's single-threaded event loop would let one slow synchronous request
stall every other greenlet in the process, including every open SSE stream.

    gunicorn -w 1 --worker-class gevent -b 0.0.0.0:$PORT sse_wsgi:app

No shared memory with the main app process (same pattern as broker-worker):
this process runs its own live_price/live_depth pollers against Supabase.
"""
from flask import Flask, jsonify

from services.broker import live_depth
from services.broker import live_price
from sse import bp as sse_bp

app = Flask(__name__)
app.register_blueprint(sse_bp)

live_price.start()
live_depth.start()


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})
