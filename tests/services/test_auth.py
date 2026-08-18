"""services/auth.py's JWKS client construction.

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


@pytest.fixture(autouse=True)
def _clear_local_mode_env(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("LOCAL_USER_ID", raising=False)


def test_local_mode_false_when_app_env_unset_even_with_local_user_id(monkeypatch):
    monkeypatch.setenv("LOCAL_USER_ID", "test-user")
    assert auth.local_mode() is False


def test_local_mode_true_for_app_env_local_with_local_user_id(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_USER_ID", "test-user")
    assert auth.local_mode() is True


def test_local_mode_false_for_app_env_production_with_local_user_id(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOCAL_USER_ID", "test-user")
    assert auth.local_mode() is False


def test_local_mode_false_for_app_env_production_without_local_user_id(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert auth.local_mode() is False
