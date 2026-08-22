#!/usr/bin/env bash
# Build and install the Rust IV engine (rust/warrants_core -> the `warrants_core`
# extension module). Installs a rustup toolchain first when the host has none.
#
# Deliberately best-effort: `logic/iv_engine.py` falls back to the pure-Python
# reference in `logic/bs_python.py` when the extension is missing, so a host that
# cannot build Rust still serves correct (slower) numbers. Pass --strict to fail
# the build instead.
set -uo pipefail

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRATE="$ROOT/rust/warrants_core"

fail() {
  echo "BUILD-RUST: $1" >&2
  [ "$STRICT" = "1" ] && exit 1
  echo "BUILD-RUST: continuing without the Rust engine (Python fallback)" >&2
  exit 0
}

if ! command -v cargo >/dev/null 2>&1; then
  export CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"
  export RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}"
  echo "BUILD-RUST: installing rustup toolchain into $CARGO_HOME"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path \
    || fail "rustup install failed"
  export PATH="$CARGO_HOME/bin:$PATH"
fi

command -v cargo >/dev/null 2>&1 || fail "cargo still not on PATH"
echo "BUILD-RUST: $(cargo --version)"

# pip drives the maturin PEP-517 backend declared in the crate's pyproject.toml.
python -m pip install --no-cache-dir "maturin>=1.7,<2.0" || fail "maturin install failed"
python -m pip install --no-cache-dir "$CRATE" || fail "warrants_core build failed"

cd "$ROOT" || fail "cannot cd to $ROOT"
python - <<'PY' || fail "warrants_core imported but did not verify"
from logic import iv_engine  # noqa: E402
assert iv_engine.RUST_AVAILABLE, iv_engine.RUST_IMPORT_ERROR
print(f"BUILD-RUST: ok -> {iv_engine.engine_info()}")
PY
