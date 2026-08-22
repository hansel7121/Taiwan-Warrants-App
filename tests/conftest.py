"""Shared pytest wiring: fixture paths and the Rust-engine skip marker."""
import pytest

from pathlib import Path

from logic import iv_engine

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
