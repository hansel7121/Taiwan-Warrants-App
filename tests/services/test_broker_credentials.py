"""broker_credentials CRUD tests.

Real in-process calls; the only faked boundary is db._run / the supabase client,
so no Supabase (Postgres or Storage) request is ever made from a test.
"""
import pytest
from cryptography.fernet import Fernet

from services import db
from services.broker import credentials, crypto


USER = "11111111-2222-3333-4444-555555555555"
KGI_FIELDS = {"account": "A123456789", "password": "hunter2-not-in-any-log"}


class _FakeQuery:
    """Chainable stand-in for a supabase-py table query; records what it did."""

    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = {}

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        return self.table._execute(self)


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def upsert(self, row, **kw):
        return _FakeQuery(self, "upsert", row)

    def update(self, row, **kw):
        return _FakeQuery(self, "update", row)

    def select(self, cols="*", **kw):
        return _FakeQuery(self, "select", cols)

    def delete(self, **kw):
        return _FakeQuery(self, "delete")

    def _match(self, row, q):
        return all(row.get(k) == v for k, v in q.filters.items())

    def _execute(self, q):
        self.store.ops.append((q.op, q.payload, dict(q.filters)))
        rows = self.store.rows
        if q.op == "upsert":
            key = (q.payload["user_id"], q.payload["broker"])
            for i, row in enumerate(rows):
                if (row["user_id"], row["broker"]) == key:
                    rows[i] = {**row, **q.payload}
                    return _Result([rows[i]])
            rows.append(dict(q.payload))
            return _Result([q.payload])
        if q.op == "update":
            hit = [r for r in rows if self._match(r, q)]
            for row in hit:
                row.update(q.payload)
            return _Result(hit)
        if q.op == "delete":
            keep = [r for r in rows if not self._match(r, q)]
            gone = [r for r in rows if self._match(r, q)]
            self.store.rows[:] = keep
            return _Result(gone)
        return _Result([r for r in rows if self._match(r, q)])


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeBucket:
    def __init__(self, store):
        self.store = store

    def upload(self, path, file, file_options=None):
        self.store.objects[path] = file
        return {"path": path}

    def download(self, path):
        return self.store.objects[path]


class _FakeStorage:
    def __init__(self, store):
        self.store = store

    def from_(self, bucket):
        self.store.buckets.append(bucket)
        return _FakeBucket(self.store)


class _FakeClient:
    def __init__(self, store):
        self.store = store
        self.storage = _FakeStorage(store)

    def table(self, name):
        self.store.tables.append(name)
        return _FakeTable(self.store, name)


class _Store:
    def __init__(self):
        self.rows = []
        self.ops = []
        self.tables = []
        self.buckets = []
        self.objects = {}


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(db, "_run", lambda build: build(_FakeClient(s)))
    monkeypatch.setenv("BROKER_CRED_KEY", Fernet.generate_key().decode())
    crypto._reset_fernet()
    yield s
    crypto._reset_fernet()


def test_upsert_persists_only_ciphertext(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)

    assert store.tables == ["broker_credentials"]
    row = store.rows[0]
    assert row["user_id"] == USER and row["broker"] == "kgi"
    assert "password" not in row
    assert KGI_FIELDS["password"] not in str(row)
    assert KGI_FIELDS["account"] not in str(row)


def test_get_credential_round_trips(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)

    got = credentials.get_credential(USER, "kgi")
    assert got["account"] == KGI_FIELDS["account"]
    assert got["password"] == KGI_FIELDS["password"]
    assert got["kgi_symbols_per_connection"] == 30
    assert got["kgi_connections"] == 2


def test_get_credential_missing_returns_none(store):
    assert credentials.get_credential(USER, "fubon") is None


def test_kgi_defaults_to_base_tier_and_is_overridable(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)
    assert store.rows[0]["kgi_symbols_per_connection"] == 30
    assert store.rows[0]["kgi_connections"] == 2

    credentials.upsert_credential(
        USER, "kgi", KGI_FIELDS,
        kgi_symbols_per_connection=100, kgi_connections=5,
    )
    assert len(store.rows) == 1
    assert store.rows[0]["kgi_symbols_per_connection"] == 100
    assert store.rows[0]["kgi_connections"] == 5


def test_fubon_row_carries_no_tier(store):
    credentials.upsert_credential(
        USER, "fubon", {"api_key": "k", "api_secret": "s"})
    row = store.rows[0]
    assert row["kgi_symbols_per_connection"] is None
    assert row["kgi_connections"] is None


def test_list_credentials_never_exposes_secrets(store, capsys):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)
    credentials.upsert_credential(
        USER, "fubon", {"api_key": "k", "api_secret": "s"})

    listed = credentials.list_credentials(USER)

    assert sorted(r["broker"] for r in listed) == ["fubon", "kgi"]
    for r in listed:
        assert "encrypted_fields" not in r
        assert set(r) == {
            "broker", "kgi_symbols_per_connection", "kgi_connections",
            "has_cert", "created_at", "updated_at",
        }
        assert r["has_cert"] is False
    assert KGI_FIELDS["password"] not in str(listed)
    assert KGI_FIELDS["password"] not in capsys.readouterr().out


def test_list_credentials_is_scoped_to_the_user(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)
    credentials.upsert_credential("other-user", "fubon", {"api_key": "k"})

    assert [r["broker"] for r in credentials.list_credentials(USER)] == ["kgi"]


def test_remove_credential_deletes_only_that_broker(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)
    credentials.upsert_credential(USER, "fubon", {"api_key": "k"})

    credentials.remove_credential(USER, "kgi")

    assert [r["broker"] for r in store.rows] == ["fubon"]


def test_cert_upload_sets_path_and_downloads_back(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)

    credentials.upload_cert(USER, "kgi", b"cert-bytes", "pfx")

    assert store.buckets == ["broker-certs"]
    expected = f"{USER}/kgi/cert.pfx"
    assert store.objects[expected] == b"cert-bytes"
    assert store.rows[0]["cert_path"] == expected
    assert credentials.list_credentials(USER)[0]["has_cert"] is True
    assert credentials.download_cert(USER, "kgi") == b"cert-bytes"


def test_download_cert_returns_none_without_a_cert(store):
    credentials.upsert_credential(USER, "kgi", KGI_FIELDS)
    assert credentials.download_cert(USER, "kgi") is None
    assert credentials.download_cert(USER, "fubon") is None
