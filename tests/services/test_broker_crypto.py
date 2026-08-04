"""Fernet round-trip for broker credentials (issue #21's validation criterion).

Real in-process calls; the only thing faked is BROKER_CRED_KEY via monkeypatch
so the test never depends on a deployment's real key.
"""
import importlib

import pytest
from cryptography.fernet import Fernet

from services.broker import crypto


SECRET = "A123456789-super-secret-password"
SAMPLE = {"account": "A123456789", "password": SECRET}


@pytest.fixture
def keyed(monkeypatch):
    """A fresh generated key, with the module's lazy cache cleared around it."""
    monkeypatch.setenv("BROKER_CRED_KEY", Fernet.generate_key().decode())
    crypto._reset_fernet()
    yield
    crypto._reset_fernet()


def test_round_trip_returns_the_exact_dict(keyed):
    token = crypto.encrypt(SAMPLE)
    assert isinstance(token, str)
    assert SECRET not in token
    assert crypto.decrypt(token) == SAMPLE


def test_nothing_is_printed_or_logged(keyed, capsys, caplog):
    caplog.set_level(0)
    token = crypto.encrypt(SAMPLE)
    crypto.decrypt(token)

    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert token not in captured.out
    assert SECRET not in caplog.text


def test_missing_key_raises_clearly(monkeypatch):
    monkeypatch.delenv("BROKER_CRED_KEY", raising=False)
    crypto._reset_fernet()
    with pytest.raises(RuntimeError, match="BROKER_CRED_KEY not configured"):
        crypto.encrypt(SAMPLE)
    crypto._reset_fernet()


def test_key_is_read_lazily_not_at_import(monkeypatch):
    """Importing must not require the env var — mirrors db.py's lazy client."""
    monkeypatch.delenv("BROKER_CRED_KEY", raising=False)
    importlib.reload(crypto)


def test_a_foreign_key_cannot_decrypt(keyed, monkeypatch):
    from cryptography.fernet import InvalidToken

    token = crypto.encrypt(SAMPLE)
    monkeypatch.setenv("BROKER_CRED_KEY", Fernet.generate_key().decode())
    crypto._reset_fernet()
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)
