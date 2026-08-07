"""Broker credential CRUD routes (issue #42) through Flask's test client.

The routes are exercised end to end: the real services/broker/credentials.py and
crypto.py run, and the only faked boundary is db._run / the supabase client, so
no Supabase request is ever made. The fakes are reused from
test_broker_credentials.py rather than duplicated, so both suites agree on what
a stored row looks like.

Auth uses the app's existing local_mode path (LOCAL_USER_ID + no RENDER) — no
new auth mechanism, and swapping LOCAL_USER_ID between requests is how the
"second user is independent" case is expressed.
"""
import io

import pytest
from cryptography.fernet import Fernet

from services import db
from services.broker import credentials, crypto

from tests.services.test_broker_credentials import _FakeClient, _Store


USER = "11111111-2222-3333-4444-555555555555"
OTHER_USER = "99999999-8888-7777-6666-555555555555"

KGI_FORM = {"broker": "kgi", "person_id": "A123456789",
            "person_pwd": "kgi-pw-not-in-any-log"}
FUBON_FORM = {"broker": "fubon", "person_id": "B987654321",
              "password": "fubon-pw-not-in-any-log",
              "cert_pass": "cert-pw-not-in-any-log"}


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    app_module.app.config["TESTING"] = True
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOCAL_USER_ID", USER)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    monkeypatch.setenv("BROKER_CRED_KEY", Fernet.generate_key().decode())
    crypto._reset_fernet()
    yield s
    crypto._reset_fernet()


def _save(client, form, cert=None):
    data = dict(form)
    if cert is not None:
        data["cert"] = cert
    return client.post("/save_broker_credential", data=data,
                       content_type="multipart/form-data")


# ── saving ───────────────────────────────────────────────────────────────

def test_save_kgi_stores_only_ciphertext(client, store):
    r = _save(client, KGI_FORM)

    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    row = store.rows[0]
    assert row["user_id"] == USER and row["broker"] == "kgi"
    assert KGI_FORM["person_pwd"] not in str(row)
    assert KGI_FORM["person_id"] not in str(row)


def test_save_kgi_defaults_the_capacity_tier_when_omitted(client, store):
    _save(client, KGI_FORM)

    assert store.rows[0]["symbols_per_connection"] == 30
    assert store.rows[0]["connections"] == 2


def test_save_kgi_accepts_a_capacity_tier_override(client, store):
    _save(client, {**KGI_FORM, "symbols_per_connection": "50", "connections": "4"})

    assert store.rows[0]["symbols_per_connection"] == 50
    assert store.rows[0]["connections"] == 4


def test_save_fubon_defaults_its_fixed_tier_and_ignores_overrides(client, store):
    # Fubon's tier is fixed for every account, so the form shows no tier fields
    # and a hand-crafted override must not take.
    _save(client, {**FUBON_FORM, "symbols_per_connection": "999", "connections": "9"})

    assert store.rows[0]["symbols_per_connection"] == 200
    assert store.rows[0]["connections"] == 5


def test_save_fubon_stores_the_uploaded_pfx(client, store):
    cert = (io.BytesIO(b"pfx-bytes"), "mycert.pfx")

    r = _save(client, FUBON_FORM, cert=cert)

    assert r.status_code == 200
    assert store.buckets == ["broker-certs"]
    assert store.objects[f"{USER}/fubon/cert.pfx"] == b"pfx-bytes"
    assert store.rows[0]["cert_path"] == f"{USER}/fubon/cert.pfx"


def test_save_fubon_accepts_a_p12_cert(client, store):
    cert = (io.BytesIO(b"p12-bytes"), "mycert.p12")

    r = _save(client, FUBON_FORM, cert=cert)

    assert r.status_code == 200
    assert store.objects[f"{USER}/fubon/cert.pfx"] == b"p12-bytes"


def test_save_fubon_without_a_cert_is_allowed(client, store):
    # Re-saving the text fields must not force a re-upload of an already
    # stored cert.
    assert _save(client, FUBON_FORM).status_code == 200
    assert store.rows[0].get("cert_path") is None
    assert store.buckets == []


def test_save_rejects_a_non_pfx_cert(client, store):
    cert = (io.BytesIO(b"nope"), "mycert.pem")

    r = _save(client, FUBON_FORM, cert=cert)

    assert r.status_code == 400
    assert store.rows == []


def test_save_rejects_a_cert_for_kgi(client, store):
    # KGI's cert is a manual CLI step outside the app; accepting one here would
    # imply the app did something with it.
    cert = (io.BytesIO(b"pfx-bytes"), "mycert.pfx")

    r = _save(client, KGI_FORM, cert=cert)

    assert r.status_code == 400
    assert store.rows == []


def test_save_rejects_an_unknown_broker(client, store):
    r = _save(client, {**KGI_FORM, "broker": "yuanta"})

    assert r.status_code == 400
    assert store.rows == []


@pytest.mark.parametrize("missing", ["person_id", "person_pwd"])
def test_save_kgi_requires_every_field(client, store, missing):
    form = {k: v for k, v in KGI_FORM.items() if k != missing}

    r = _save(client, form)

    assert r.status_code == 400
    assert store.rows == []


@pytest.mark.parametrize("missing", ["person_id", "password", "cert_pass"])
def test_save_fubon_requires_every_field(client, store, missing):
    form = {k: v for k, v in FUBON_FORM.items() if k != missing}

    r = _save(client, form)

    assert r.status_code == 400
    assert store.rows == []


def test_save_rejects_a_blank_field(client, store):
    r = _save(client, {**KGI_FORM, "person_pwd": "   "})

    assert r.status_code == 400
    assert store.rows == []


def test_save_rejects_a_nonsense_capacity_tier(client, store):
    assert _save(client, {**KGI_FORM, "connections": "zero"}).status_code == 400
    assert _save(client, {**KGI_FORM, "connections": "0"}).status_code == 400
    assert store.rows == []


def test_saving_twice_updates_the_one_row(client, store):
    _save(client, KGI_FORM)
    _save(client, {**KGI_FORM, "person_pwd": "rotated-pw"})

    assert len(store.rows) == 1
    assert credentials.get_credential(USER, "kgi")["person_pwd"] == "rotated-pw"


def test_no_route_response_ever_echoes_a_secret(client, store):
    body = _save(client, KGI_FORM).get_data(as_text=True)
    assert KGI_FORM["person_pwd"] not in body

    listed = client.get("/list_broker_credentials").get_data(as_text=True)
    assert KGI_FORM["person_pwd"] not in listed
    assert "encrypted_fields" not in listed


# ── listing / removing ───────────────────────────────────────────────────

def test_list_returns_metadata_only(client, store):
    _save(client, KGI_FORM)

    r = client.get("/list_broker_credentials")

    assert r.status_code == 200
    rows = r.get_json()
    assert len(rows) == 1
    assert set(rows[0]) == {"broker", "symbols_per_connection", "connections",
                            "has_cert", "created_at", "updated_at"}
    assert rows[0]["broker"] == "kgi"


def test_list_is_empty_before_anything_is_saved(client, store):
    assert client.get("/list_broker_credentials").get_json() == []


def test_remove_deletes_only_that_broker(client, store):
    _save(client, KGI_FORM)
    _save(client, FUBON_FORM)

    r = client.post("/remove_broker_credential", json={"broker": "kgi"})

    assert r.status_code == 200
    assert [row["broker"] for row in store.rows] == ["fubon"]


def test_remove_rejects_an_unknown_broker(client, store):
    r = client.post("/remove_broker_credential", json={"broker": "yuanta"})

    assert r.status_code == 400


# ── per-user scoping ─────────────────────────────────────────────────────

def test_a_second_user_saves_independently(client, store, monkeypatch):
    _save(client, KGI_FORM)

    monkeypatch.setenv("LOCAL_USER_ID", OTHER_USER)
    _save(client, {**KGI_FORM, "person_id": "C555555555",
                   "person_pwd": "other-users-pw"})

    assert len(store.rows) == 2
    assert {row["user_id"] for row in store.rows} == {USER, OTHER_USER}
    assert credentials.get_credential(USER, "kgi")["person_pwd"] == \
        KGI_FORM["person_pwd"]
    assert credentials.get_credential(OTHER_USER, "kgi")["person_pwd"] == \
        "other-users-pw"


def test_list_only_shows_the_calling_users_credentials(client, store, monkeypatch):
    _save(client, KGI_FORM)

    monkeypatch.setenv("LOCAL_USER_ID", OTHER_USER)
    _save(client, FUBON_FORM)

    assert [r["broker"] for r in client.get("/list_broker_credentials").get_json()] \
        == ["fubon"]


# ── sub-tab shell ────────────────────────────────────────────────────────
# No browser is available here, so the shell is checked at the render seam:
# the markup and the script that drives it must actually reach the page.

def test_index_renders_both_scanner_sub_tabs(client):
    html = client.get("/").get_data(as_text=True)

    assert 'switchScannerSub(\'screener\'' in html
    assert 'switchScannerSub(\'live\'' in html
    assert ">Screener<" in html
    assert ">Live warrant<" in html
    assert 'id="scsub-screener"' in html and 'id="scsub-live"' in html


def test_index_ships_the_live_warrant_script_and_form(client):
    html = client.get("/").get_data(as_text=True)

    assert "js/live_warrant.js" in html
    # Broker selector plus every field the two brokers need between them.
    for element_id in ("bcBroker", "bcPersonId", "bcPersonPwd", "bcPassword",
                       "bcCertPass", "bcCert", "bcSymbols", "bcConnections"):
        assert f'id="{element_id}"' in html
    assert 'accept=".p12,.pfx"' in html


def test_remove_cannot_touch_another_users_credential(client, store, monkeypatch):
    _save(client, KGI_FORM)

    monkeypatch.setenv("LOCAL_USER_ID", OTHER_USER)
    client.post("/remove_broker_credential", json={"broker": "kgi"})

    assert [row["user_id"] for row in store.rows] == [USER]
