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

# An extension built before the whole-chain static-arb scan landed still serves
# every other kernel, so it skips these rather than failing them.
static_arb_rust = pytest.mark.skipif(
    not iv_engine.SCAN_STATIC_ARB,
    reason="warrants_core predates scan_static_arb — rebuild with scripts/build_rust.sh",
)


@pytest.fixture(scope="session")
def fixture_root():
    return FIXTURE_ROOT
