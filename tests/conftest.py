"""Shared pytest wiring: fixture paths and the Rust-engine skip marker."""
import pytest

from pathlib import Path

from logic import iv_engine
from services import tick_recorder

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

# Applied to any test that needs the compiled extension; the Python half of every
# parity test stays unguarded so a machine without a toolchain still exercises
# the fallback.
rust_only = pytest.mark.skipif(
    not iv_engine.RUST_AVAILABLE,
    reason=f"warrants_core not built ({iv_engine.RUST_IMPORT_ERROR})",
)


@pytest.fixture(scope="session")
def fixture_root():
    return FIXTURE_ROOT


@pytest.fixture(autouse=True)
def _no_tick_recording(monkeypatch):
    """Off by default for every test. The Live tab tick tests drive
    `_handle_message` directly, which now records — left enabled, a suite run
    during TW-equity hours would litter the repo with real tick files.
    tests/services/test_tick_recorder.py re-enables it against a tmp dir.
    """
    monkeypatch.setattr(tick_recorder, "ENABLED", False)
