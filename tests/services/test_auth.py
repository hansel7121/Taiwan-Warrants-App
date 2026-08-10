"""services/auth.py's JWKS client construction (issue #57 part 2).

PyJWKClient defaults to a 30s network timeout with no override; on a single
gthread-worker process, a slow/flaky Supabase auth backend can then pin a
request thread for up to 30s per hit, starving /healthz. This bounds it.
"""
import pytest

from services import auth


@pytest.fixture(autouse=True)
def _reset_jwks_client(monkeypatch):
    monkeypatch.setattr(auth, "_jwks_client", None)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    yield
    monkeypatch.setattr(auth, "_jwks_client", None)


def test_jwks_client_has_a_bounded_timeout():
    client = auth._jwks()
    assert client.timeout <= 5


def test_jwks_client_is_cached_across_calls():
    first = auth._jwks()
    second = auth._jwks()
    assert first is second
