"""services/db.py's client lifecycle: shared-singleton locking, the configured
request timeout, and Conn (issue #56 — 120s default timeout + unlocked rebuild
+ one shared client let a poller hang unrelated user requests for up to 2 min).

_build_client is monkeypatched everywhere here so no real Supabase client (and
no real network) is ever constructed.
"""
import threading
import time

import httpx
import pytest

from services import db


class _FakeClient:
    """Stands in for a supabase-py Client: just enough surface for db._run
    and db.Conn.run to call .table(...).execute() through."""
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def table(self, name):
        return self

    def execute(self):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise httpx.RemoteProtocolError("Server disconnected")
        return "ok"


@pytest.fixture(autouse=True)
def _restore_client():
    """db._client is a module-level singleton; leave it as found."""
    original = db._client
    yield
    db._client = original


# -- timeout ------------------------------------------------------------------

def test_build_client_passes_a_short_postgrest_timeout(monkeypatch):
    """The supabase-py default (ClientOptions.postgrest_client_timeout) is 120s;
    a stuck connection would block a thread that long. Assert we override it to
    something short, not left at the default."""
    captured = {}

    def fake_create_client(url, key, options=None):
        captured["timeout"] = options.postgrest_client_timeout
        return _FakeClient()

    monkeypatch.setattr(db, "create_client", fake_create_client)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "key")

    db._build_client()

    assert 0 < captured["timeout"] < 120


# -- locked rebuild -------------------------------------------------------------

def test_concurrent_client_calls_after_a_reset_build_only_once(monkeypatch):
    """client()/_reset_client() must be locked: two threads racing a rebuild
    must not both call create_client and clobber each other's result."""
    build_count = {"n": 0}
    build_lock_gate = threading.Event()

    def slow_build():
        build_count["n"] += 1
        build_lock_gate.wait(timeout=1)  # widen the race window
        return _FakeClient()

    monkeypatch.setattr(db, "_build_client", slow_build)
    db._client = None

    threads = [threading.Thread(target=db.client) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    build_lock_gate.set()
    for t in threads:
        t.join(timeout=2)

    assert build_count["n"] == 1


# -- Conn (dedicated, isolated client) ------------------------------------------

def test_conn_is_built_lazily(monkeypatch):
    """No creds required to instantiate one — only its first .run() call —
    so `db.Conn()` at module load (live_price.py / live_depth.py) can't fail
    just because Supabase isn't configured yet."""
    monkeypatch.setattr(db, "_build_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("Supabase not configured")))

    conn = db.Conn()

    assert conn._client is None


def test_conn_client_is_not_the_shared_singleton(monkeypatch):
    monkeypatch.setattr(db, "_build_client", lambda: _FakeClient())
    db._client = _FakeClient()

    conn = db.Conn()
    conn.run(lambda c: None)

    assert conn._client is not db._client


def test_conn_run_retries_once_on_transport_error(monkeypatch):
    fake = _FakeClient(fail_times=1)
    monkeypatch.setattr(db, "_build_client", lambda: fake)

    conn = db.Conn()
    result = conn.run(lambda c: c.table("t").execute())

    assert result == "ok"
    assert fake.calls == 2


def test_conn_run_propagates_a_second_transport_error(monkeypatch):
    def always_fail():
        return _FakeClient(fail_times=99)

    monkeypatch.setattr(db, "_build_client", always_fail)

    conn = db.Conn()
    with pytest.raises(httpx.TransportError):
        conn.run(lambda c: c.table("t").execute())
