"""Live-price SSE feed (issue #46), as a Flask Blueprint.

Split out from app.py (issue #58 item 1) so this route can run as its own
lightweight service (sse_wsgi.py) instead of holding a gunicorn thread per
open Live Warrant tab on the main app's pool for the connection's whole life.
Still mounted into app.py too, for local dev — same-origin there, so the CORS
header below is a no-op (browsers don't check it same-origin).
"""
import json
import os
import threading
import time

from flask import Blueprint, Response, jsonify, stream_with_context

from services import auth
from services.broker import desired_state, live_assignment, live_depth, live_price, watchlist

bp = Blueprint("sse", __name__)

LIVE_PRICES_SSE_SEC = 1
_SSE_MAX_STREAMS = int(os.environ.get("LIVE_PRICES_SSE_MAX_STREAMS", "4"))
_sse_slots = threading.BoundedSemaphore(_SSE_MAX_STREAMS)

# Set only when this blueprint runs as the standalone SSE service
# (sse_wsgi.py in production): the main app's own origin, so its
# cross-origin EventSource request is allowed. Empty (default) adds no
# header, which is correct for the same-origin app.py mount.
_CORS_ORIGIN = os.environ.get("SSE_CORS_ORIGIN", "")


@bp.after_request
def _add_cors_header(response):
    if _CORS_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = _CORS_ORIGIN
    return response


@bp.route("/live_prices/stream")
def live_prices_stream():
    """Push the live-price cache to the browser once a second, forever.

    Not @require_auth: EventSource cannot send an Authorization header, so the
    token arrives as ?token= and is checked by hand here.

    Capped at `_SSE_MAX_STREAMS` concurrent streams (env
    `LIVE_PRICES_SSE_MAX_STREAMS`, default 4) so a burst of open tabs cannot
    exhaust every gunicorn thread and starve the rest of the app (single
    worker, 8 threads — CLAUDE.md).
    """
    _user, err = auth.authenticate_for_stream()
    if err:
        body, status = err
        return jsonify(body), status

    if not _sse_slots.acquire(blocking=False):
        return jsonify({"error": "stream_capacity"}), 503

    def gen():
        try:
            while True:
                yield _live_prices_event()
                time.sleep(LIVE_PRICES_SSE_SEC)
        finally:
            _sse_slots.release()

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


def _live_prices_event():
    """One SSE frame: every watched code that has ever ticked, plus is_live.

    Codes never seen are omitted entirely rather than sent as null (ADR-0005 —
    "nothing yet" is not a price), so an all-quiet Watchlist emits `data: {}`;
    the client keeps whatever it last drew.

    is_live comes from the Worker Status of the connection carrying the code,
    not from tick age. The placement is read from `live_assignment` — what the
    worker actually published — rather than recomputed here, because the
    worker's intraday reassign() is sticky and a fresh assign() would name the
    wrong connection. A cached code the worker is not carrying is not live.
    """
    codes = watchlist.list_codes()
    prices = live_price.snapshot(codes)
    depths = live_depth.snapshot(codes)
    live = {}
    if prices:
        reported = {(row["user_id"], row["broker"]): row["status"]
                    for row in desired_state.list_all_worker_status()}
        live = {code: reported.get((user_id, broker)) == "connected"
                for code, (broker, user_id) in live_assignment.read_all().items()}

    payload = {
        code: {
            "price": value["price"],
            "ts": value["ts"].isoformat(),
            "broker": value["broker"],
            "qty": value.get("qty"),
            "instrument": value.get("instrument", "warrant"),  # "warrant" or "tw_option" (#55)
            "is_live": bool(live.get(code)),
            "depth": _depth_payload(depths.get(code)),
        }
        for code, value in prices.items()
    }
    return f"data: {json.dumps(payload)}\n\n"


def _depth_payload(depth):
    """A code's depth snapshot as JSON-safe fields, or None if never seen (#51)."""
    if depth is None:
        return None
    return {
        "bid_prices": depth["bid_prices"],
        "bid_volumes": depth["bid_volumes"],
        "ask_prices": depth["ask_prices"],
        "ask_volumes": depth["ask_volumes"],
    }
