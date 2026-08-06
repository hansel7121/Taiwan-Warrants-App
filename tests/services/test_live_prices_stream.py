"""GET /live_prices/stream — the Live sub-tab's SSE price feed (issue #46).

Same shape as test_broker_status_route.py: the real watchlist, pool,
credentials and live_price modules run, and the only faked boundary is
db._run / the supabase client (fakes borrowed from test_watchlist.py).

The route's generator is an infinite loop, so it is never driven here — the
tests call the single-frame builder `app._live_prices_event()` directly, the
way test_live_price.py pins `_poll_once` instead of the poll thread. The
route function itself is only exercised for its auth/response envelope.
"""
import json

import pytest

import app as app_module
from services import auth, db
from services.broker import desired_state, live_depth, live_price

from tests.services.test_watchlist import _FakeClient, _Store


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"

TS = "2026-08-04T13:29:59+08:00"


@pytest.fixture
def store(monkeypatch):
    live_price._cache.invalidate()
    live_depth._cache.invalidate()
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    yield s
    live_price._cache.invalidate()
    live_depth._cache.invalidate()


@pytest.fixture
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", USER)
    with app_module.app.test_client() as c:
        yield c


def _watch(store, *codes):
    for code in codes:
        store.table("watchlist").append({"code": code, "added_by": USER})


def _tick(store, code, price=1.23, ts=TS, broker="kgi", qty=None):
    """Put a tick in the web process's cache the way the poller would."""
    store.table("live_prices").append(
        {"code": code, "price": price, "ts": ts, "broker": broker, "qty": qty})
    live_price._poll_once()


def _depth(store, code, broker="kgi"):
    """Put a depth snapshot in the web process's cache the way the poller would."""
    store.table("live_depth").append({
        "code": code,
        "bid_prices": [1.20, 1.19, 1.18, 1.17, 1.16],
        "bid_volumes": [10, 20, 30, 40, 50],
        "ask_prices": [1.21, 1.22, 1.23, 1.24, 1.25],
        "ask_volumes": [5, 15, 25, 35, 45],
        "ts": TS,
        "broker": broker,
    })
    live_depth._poll_once()


def _assign(store, code, broker="kgi", user_id=USER, connection_index=0):
    """Put a code in the web process's view of the worker's live_assignment table."""
    store.table("live_assignment").append(
        {"code": code, "broker": broker, "user_id": user_id,
         "connection_index": connection_index})


def _event():
    """The decoded payload of one SSE frame."""
    raw = app_module._live_prices_event()
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    return json.loads(raw[len("data: "):])


# -- which codes appear -----------------------------------------------------

def test_a_watched_code_that_has_never_ticked_is_absent(store):
    """"No tick yet" shows nothing (ADR-0005), so it must not arrive as a null
    row the client would draw as a blank price."""
    store.credit(USER, "kgi")
    _watch(store, "030001")

    assert _event() == {}


def test_a_ticked_code_carries_price_ts_and_broker(store):
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001", price=2.5, broker="fubon")

    row = _event()["030001"]
    assert row["price"] == 2.5
    assert row["broker"] == "fubon"


def test_a_ticked_code_carries_qty(store):
    """Issue #54: the SSE frame is the trade-log panel's only source, so qty
    has to reach it the same as price does."""
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001", qty=5000)

    assert _event()["030001"]["qty"] == 5000


def test_a_ticked_code_with_no_depth_yet_has_a_null_depth(store):
    """Issue #51: Fubon-carried codes have no depth feed at all yet, and a
    KGI code that has only ticked so far has none until its first bidask
    message — both must render as "no depth" rather than a crash."""
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001")

    assert _event()["030001"]["depth"] is None


def test_a_ticked_code_with_depth_carries_all_four_arrays(store):
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001")
    _depth(store, "030001")

    depth = _event()["030001"]["depth"]
    assert depth["bid_prices"] == [1.20, 1.19, 1.18, 1.17, 1.16]
    assert depth["bid_volumes"] == [10, 20, 30, 40, 50]
    assert depth["ask_prices"] == [1.21, 1.22, 1.23, 1.24, 1.25]
    assert depth["ask_volumes"] == [5, 15, 25, 35, 45]


def test_an_empty_watchlist_emits_an_empty_frame(store):
    """Still a frame, not a skipped yield — the stream stays chatty enough for
    the browser to notice it is alive."""
    assert _event() == {}


def test_every_ticked_code_is_in_one_frame(store):
    store.credit(USER, "kgi", symbols_per_connection=4, connections=1)
    _watch(store, "030001", "030002", "030003")
    _tick(store, "030001")
    _tick(store, "030002", price=4.5)

    assert set(_event()) == {"030001", "030002"}


# -- is_live comes from Worker Status ---------------------------------------
#
# Placement is read from live_assignment — what the worker published — so these
# seed that table directly rather than letting a recomputed assign() guess it.

def test_a_code_on_a_connected_account_is_live(store):
    _watch(store, "030001")
    _tick(store, "030001")
    _assign(store, "030001")
    desired_state.set_worker_status(USER, "kgi", "connected")

    assert _event()["030001"]["is_live"] is True


def test_a_code_on_an_account_that_is_not_connected_is_not_live(store):
    _watch(store, "030001")
    _tick(store, "030001")
    _assign(store, "030001")
    desired_state.set_worker_status(USER, "kgi", "reconnecting")

    assert _event()["030001"]["is_live"] is False


def test_a_never_reported_account_is_not_live(store):
    _watch(store, "030001")
    _tick(store, "030001")
    _assign(store, "030001")

    assert _event()["030001"]["is_live"] is False


def test_a_stale_price_is_still_sent_when_not_live(store):
    """ADR-0005: a dropped connection marks the row stale, it does not clear
    it — so the last tick has to survive is_live going false."""
    _watch(store, "030001")
    _tick(store, "030001", price=7.5)
    _assign(store, "030001")
    desired_state.set_worker_status(USER, "kgi", "disconnected")

    row = _event()["030001"]
    assert row["price"] == 7.5 and row["is_live"] is False


def test_liveness_is_per_connection_not_global(store):
    """Two accounts, one healthy and one not, and one code on each."""
    _watch(store, "030001", "030002")
    _tick(store, "030001")
    _tick(store, "030002")
    _assign(store, "030001")
    _assign(store, "030002", broker="fubon", user_id=OTHER)
    desired_state.set_worker_status(USER, "kgi", "connected")
    desired_state.set_worker_status(OTHER, "fubon", "reconnecting")

    live = {code: row["is_live"] for code, row in _event().items()}
    assert live == {"030001": True, "030002": False}


def test_a_cached_code_the_worker_is_not_carrying_is_not_live(store):
    """Dropped from the Watchlist mid-stream, or past the pool's capacity: the
    price is still cached but nothing published a connection for it."""
    _watch(store, "030001", "030002")
    _tick(store, "030001")
    _tick(store, "030002")
    _assign(store, "030001")
    desired_state.set_worker_status(USER, "kgi", "connected")

    assert _event()["030002"]["is_live"] is False


def test_a_sticky_placement_is_believed_over_a_repacked_one(store):
    """The bug this table exists for: after an intraday edit the worker's
    reassign() keeps a code where it is, and a fresh assign() would put it on
    the first account instead — and read the wrong account's status."""
    _watch(store, "030001")
    _tick(store, "030001")
    _assign(store, "030001", broker="fubon", user_id=OTHER)
    desired_state.set_worker_status(USER, "kgi", "connected")
    desired_state.set_worker_status(OTHER, "fubon", "reconnecting")

    assert _event()["030001"]["is_live"] is False


def test_liveness_is_not_tick_recency(store):
    """A code far past the cache TTL is live while its connection is up."""
    _watch(store, "030001")
    _tick(store, "030001", ts="2020-01-01T09:00:00+08:00")
    _assign(store, "030001")
    desired_state.set_worker_status(USER, "kgi", "connected")

    assert _event()["030001"]["is_live"] is True


# -- serialization ----------------------------------------------------------

def test_the_timestamp_is_sent_as_a_parseable_string(store):
    """JSON has no datetime; the client needs the offset kept to show a Taipei
    trade time rather than reading it as UTC."""
    from datetime import datetime

    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001", ts=TS)

    sent = _event()["030001"]["ts"]
    assert isinstance(sent, str)
    assert datetime.fromisoformat(sent) == datetime.fromisoformat(TS)


def test_a_frame_is_one_sse_data_event(store):
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001")

    raw = app_module._live_prices_event()

    assert raw.startswith("data: ")
    assert raw.endswith("\n\n")
    assert raw.count("\n\n") == 1


# -- the route never writes -------------------------------------------------

def test_building_a_frame_writes_nothing(store):
    store.credit(USER, "kgi")
    _watch(store, "030001")
    _tick(store, "030001")
    store.ops.clear()

    _event()

    assert [op for op in store.ops if op[1] != "select"] == []


# -- auth -------------------------------------------------------------------

def test_local_mode_streams_without_a_token(client, store):
    r = client.get("/live_prices/stream")

    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    r.close()   # the generator never ends on its own


def test_no_token_is_rejected_when_auth_is_on(client, store, monkeypatch):
    """EventSource cannot send an Authorization header, so an absent ?token= is
    the missing-credential case here — not an absent Bearer header."""
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    r = client.get("/live_prices/stream")

    assert r.status_code == 401
    assert r.get_json() == {"error": "missing_token"}


def test_a_bearer_header_is_not_accepted_in_place_of_the_query_param(client, store,
                                                                    monkeypatch):
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    r = client.get("/live_prices/stream", headers={"Authorization": "Bearer abc"})

    assert r.status_code == 401
    assert r.get_json() == {"error": "missing_token"}


def test_unconfigured_auth_is_a_503(client, store, monkeypatch):
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)

    r = client.get("/live_prices/stream")

    assert r.status_code == 503


def test_a_bad_token_is_rejected(client, store, monkeypatch):
    """No Supabase project to verify against, so the verify step is faked at
    services.auth._verify — the mapping of failure to 401 is what matters."""
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(auth, "_verify",
                        lambda t: (_ for _ in ()).throw(ValueError("bad sig")))

    r = client.get("/live_prices/stream?token=nonsense")

    assert r.status_code == 401
    assert r.get_json() == {"error": "invalid_token"}


def test_a_valid_token_off_the_allowlist_is_forbidden(client, store, monkeypatch):
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(auth, "_verify", lambda t: {"sub": OTHER, "email": "x@y.z"})
    monkeypatch.setattr(auth, "_is_allowed", lambda email: False)

    r = client.get("/live_prices/stream?token=good")

    assert r.status_code == 403
    assert r.get_json() == {"error": "not_allowed"}


def test_an_allowlisted_token_streams(client, store, monkeypatch):
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(auth, "_verify", lambda t: {"sub": OTHER, "email": "x@y.z"})
    monkeypatch.setattr(auth, "_is_allowed", lambda email: True)

    r = client.get("/live_prices/stream?token=good")

    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    r.close()


# -- stream capacity --------------------------------------------------------

@pytest.fixture(autouse=True)
def slots(monkeypatch):
    """A fresh semaphore per test — `_sse_slots` is module-level global state,
    and the route's generator is never driven here to release it."""
    import threading

    monkeypatch.setattr(
        app_module, "_sse_slots",
        threading.BoundedSemaphore(app_module._SSE_MAX_STREAMS))
    yield app_module._sse_slots


def test_a_stream_over_capacity_is_rejected(client, store, slots):
    """Every open tab pins a gunicorn thread for its whole session, so the
    (8-thread, single-worker) app has to refuse rather than starve."""
    for _ in range(app_module._SSE_MAX_STREAMS):
        assert slots.acquire(blocking=False)

    r = client.get("/live_prices/stream")

    assert r.status_code == 503
    assert r.get_json() == {"error": "stream_capacity"}


def test_a_freed_slot_lets_the_next_stream_in(client, store, slots):
    for _ in range(app_module._SSE_MAX_STREAMS):
        assert slots.acquire(blocking=False)
    slots.release()

    r = client.get("/live_prices/stream")

    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    r.close()


def test_capacity_is_checked_after_auth(client, store, monkeypatch, slots):
    """A full pool must not turn an unauthenticated request into a 503 — the
    401 is the more useful answer, and a rejected caller takes no slot."""
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    for _ in range(app_module._SSE_MAX_STREAMS):
        assert slots.acquire(blocking=False)

    r = client.get("/live_prices/stream")

    assert r.status_code == 401
