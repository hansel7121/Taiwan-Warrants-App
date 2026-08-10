"""GET /broker/status — the Live sub-tab's connection-status panel (issue #45).

Exercised end to end through Flask's test client: the real credentials, pool,
watchlist and desired_state modules run, and the only faked boundary is
db._run / the supabase client. The fakes come from test_watchlist.py rather
than test_broker_desired_state.py because this route spans the same three
tables that suite already models — broker_credentials for capacity, watchlist
for what is subscribed, worker_status for the state.

Capacity Tiers are seeded small (2 symbols x 1 connection and friends) for the
same reason as in test_watchlist.py: the real kgi/fubon defaults are too big to
fill in a readable test.

Auth uses the app's existing local_mode path (LOCAL_USER_ID + no RENDER).
Unlike the credential routes, this one is NOT scoped to the caller — the panel
shows every account in the shared pool — so swapping LOCAL_USER_ID is how the
"same rows, different is_you" case is expressed.
"""
import pytest

from services import db
from services.broker import desired_state

from tests.services.test_watchlist import _FakeClient, _Store


USER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    return s


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    app_module.app.config["TESTING"] = True
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", USER)
    with app_module.app.test_client() as c:
        yield c


def _watch(store, *codes):
    for code in codes:
        store.table("watchlist").append({"code": code, "added_by": USER})


def _assign(store, user_id, broker, *codes):
    """Seed `live_assignment` the way the worker's publish() would: one row per
    code actually carried, independent of what pool.assign() would have packed."""
    for code in codes:
        store.table("live_assignment").append(
            {"code": code, "broker": broker, "user_id": user_id, "connection_index": 0})


def _rows(client):
    r = client.get("/broker/status")
    assert r.status_code == 200
    return r.get_json()


def _by_account(rows):
    return {(row["broker"], row["is_you"]): row for row in rows}


# ── one row per Broker Account ───────────────────────────────────────────

def test_every_stored_account_gets_a_row_across_both_users(client, store):
    store.credit(USER, "kgi")
    store.credit(USER, "fubon")
    store.credit(OTHER, "kgi")
    store.credit(OTHER, "fubon")

    rows = _rows(client)

    assert len(rows) == 4
    assert sorted((r["broker"], r["is_you"]) for r in rows) == sorted(
        [("kgi", True), ("fubon", True), ("kgi", False), ("fubon", False)])


def test_no_stored_account_means_no_rows(client, store):
    assert _rows(client) == []


def test_is_you_follows_the_caller_not_the_row(client, store, monkeypatch):
    store.credit(USER, "kgi")
    store.credit(OTHER, "fubon")

    assert _by_account(_rows(client))[("kgi", True)]["broker"] == "kgi"

    monkeypatch.setenv("LOCAL_USER_ID", OTHER)
    flipped = _by_account(_rows(client))
    assert ("fubon", True) in flipped and ("kgi", False) in flipped


def test_a_row_never_leaks_another_users_identity(client, store):
    """There is no user_id -> email mapping in the app, and the raw user_id is
    not something the panel needs — is_you is the whole label."""
    store.credit(OTHER, "kgi")

    row = _rows(client)[0]

    assert set(row) == {"broker", "is_you", "status", "subscribed", "capacity"}
    assert OTHER not in str(row)


# ── status join ──────────────────────────────────────────────────────────

def test_the_workers_reported_status_is_shown(client, store):
    store.credit(USER, "kgi")
    desired_state.set_worker_status(USER, "kgi", "connected")

    assert _rows(client)[0]["status"] == "connected"


def test_an_account_never_reported_on_has_a_null_status(client, store):
    """Not 'disconnected': a worker that has never run is a different state
    from one that ran and dropped the connection."""
    store.credit(USER, "kgi")

    assert _rows(client)[0]["status"] is None


def test_status_is_matched_per_account_not_per_broker(client, store):
    store.credit(USER, "kgi")
    store.credit(OTHER, "kgi")
    desired_state.set_worker_status(USER, "kgi", "connected")
    desired_state.set_worker_status(OTHER, "kgi", "reconnecting")

    rows = _by_account(_rows(client))
    assert rows[("kgi", True)]["status"] == "connected"
    assert rows[("kgi", False)]["status"] == "reconnecting"


# ── subscribed / capacity ────────────────────────────────────────────────

def test_capacity_is_the_accounts_whole_tier(client, store):
    store.credit(USER, "kgi", symbols_per_connection=3, connections=2)

    assert _rows(client)[0]["capacity"] == 6


def test_subscribed_sums_every_connection_of_the_account(client, store):
    """One combined count per account, not a per-connection breakdown — these
    three codes are spread across the account's connections by the worker."""
    store.credit(USER, "kgi", symbols_per_connection=2, connections=2)
    _assign(store, USER, "kgi", "A", "B", "C")

    row = _rows(client)[0]
    assert row["subscribed"] == 3
    assert row["capacity"] == 4


def test_an_idle_account_reports_zero_subscribed(client, store):
    store.credit(USER, "kgi", symbols_per_connection=2, connections=1)

    assert _rows(client)[0]["subscribed"] == 0


def test_a_credentialed_but_unconnected_account_reports_zero_even_with_room(client, store):
    """The worker's own live_assignment is the only source of truth here — an
    idle KGI credential must never claim codes it never actually opened a
    session to carry, even though pool.assign() would have packed them there
    first by broker order."""
    store.credit(USER, "kgi", symbols_per_connection=10, connections=1)
    store.credit(USER, "fubon", symbols_per_connection=10, connections=1)
    _watch(store, "A", "B", "C")
    _assign(store, USER, "fubon", "A", "B", "C")

    rows = _by_account(_rows(client))

    assert rows[("kgi", True)]["subscribed"] == 0
    assert rows[("fubon", True)]["subscribed"] == 3


def test_subscribed_counts_are_independent_per_account(client, store):
    store.credit(USER, "kgi", symbols_per_connection=2, connections=1)
    store.credit(OTHER, "kgi", symbols_per_connection=1, connections=1)
    store.credit(USER, "fubon", symbols_per_connection=3, connections=1)
    _assign(store, USER, "kgi", "A", "B")
    _assign(store, OTHER, "kgi", "C")
    _assign(store, USER, "fubon", "D")

    rows = _by_account(_rows(client))

    assert (rows[("kgi", True)]["subscribed"], rows[("kgi", True)]["capacity"]) == (2, 2)
    assert (rows[("kgi", False)]["subscribed"], rows[("kgi", False)]["capacity"]) == (1, 1)
    assert (rows[("fubon", True)]["subscribed"], rows[("fubon", True)]["capacity"]) == (1, 3)


# ── the route never writes ───────────────────────────────────────────────

def test_reading_the_panel_writes_nothing(client, store):
    store.credit(USER, "kgi")
    _rows(client)

    assert [op for op in store.ops if op[1] != "select"] == []


# ── panel markup ─────────────────────────────────────────────────────────
# No browser here, so the panel is checked at the render seam: the container
# the loader writes into has to actually reach the page.

def test_index_renders_the_connection_status_panel(client):
    html = client.get("/").get_data(as_text=True)

    assert 'id="csContainer"' in html
    assert "js/live_warrant.js" in html
