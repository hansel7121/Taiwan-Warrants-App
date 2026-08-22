"""Backend selection for the Black-Scholes IV / delta kernels.

Two interchangeable engines sit behind these names: the compiled Rust extension
``warrants_core`` (used whenever it imports) and the pure-Python/NumPy reference
in ``logic/bs_python.py`` (the backup). Both return columns that are identical
after ``round(..., 4)`` — the Rust solver reuses the same ``[1e-6, 10]`` bracket,
the same certified-Newton acceptance test, and a port of SciPy's ``brentq`` for
the exact fallback, so switching engines cannot move a displayed number.

``IV_ENGINE=rust|python|auto`` (default ``auto``) picks the backend at import.
Callers get these names re-exported from ``logic.warrant_logic``.
"""
import os

import numpy as np

from logic import bs_python

# Always available regardless of engine: trivial, no solver involved.
calc_real_leverage = bs_python.calc_real_leverage

_MODE = (os.getenv("IV_ENGINE") or "auto").strip().lower()

_rust = None
RUST_IMPORT_ERROR = None
if _MODE in ("auto", "rust"):
    try:
        import warrants_core as _rust  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on build environment
        _rust = None
        RUST_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        if _MODE == "rust":
            raise

RUST_AVAILABLE = _rust is not None
ENGINE = "rust" if RUST_AVAILABLE else "python"


def engine_info():
    """One-line engine description for boot logs and /healthz."""
    if RUST_AVAILABLE:
        return f"rust warrants_core {_rust.__version__}"
    if RUST_IMPORT_ERROR:
        return f"python (warrants_core unavailable: {RUST_IMPORT_ERROR})"
    return "python (IV_ENGINE=python)"


def _is_scalar(*vals):
    """True when every value is a plain real scalar — the only case the Rust scalar entry points accept."""
    for v in vals:
        if isinstance(v, (bool, np.bool_)):
            continue
        if isinstance(v, (int, float, np.integer, np.floating)):
            continue
        return False
    return True


def _flat(arrays, dtypes):
    """Broadcast a set of inputs together and return C-contiguous flat views plus the broadcast shape."""
    cast = [np.asarray(a, dtype=d) for a, d in zip(arrays, dtypes)]
    b = np.broadcast_arrays(*cast)
    shape = b[0].shape
    return [np.ascontiguousarray(x, dtype=d).ravel() for x, d in zip(b, dtypes)], shape


_F = float
_B = bool

if RUST_AVAILABLE:

    def bs_price(S, K, T, r, sigma, ratio, is_put=False):
        if _is_scalar(S, K, T, r, sigma, ratio, is_put):
            return _rust.bs_price(_F(S), _F(K), _F(T), _F(r), _F(sigma), _F(ratio), _B(is_put))
        return bs_python.bs_price(S, K, T, r, sigma, ratio, is_put)

    def bs_delta(S, K, T, r, sigma, ratio, is_put=False):
        if _is_scalar(S, K, T, r, sigma, ratio, is_put):
            return _rust.bs_delta(_F(S), _F(K), _F(T), _F(r), _F(sigma), _F(ratio), _B(is_put))
        return bs_python.bs_delta(S, K, T, r, sigma, ratio, is_put)

    def bs_vega(S, K, T, r, sigma, ratio):
        if _is_scalar(S, K, T, r, sigma, ratio):
            return _rust.bs_vega(_F(S), _F(K), _F(T), _F(r), _F(sigma), _F(ratio))
        return bs_python.bs_vega(S, K, T, r, sigma, ratio)

    def implied_vol(price, S, K, T, r, ratio, is_put=False):
        if _is_scalar(price, S, K, T, r, ratio, is_put):
            return _rust.implied_vol(_F(price), _F(S), _F(K), _F(T), _F(r), _F(ratio), _B(is_put))
        return bs_python.implied_vol(price, S, K, T, r, ratio, is_put)

    def implied_vol_vec(price, S, K, T, r, ratio, is_put):
        (p, s, k, t, rr, rt, ip), shape = _flat(
            (price, S, K, T, r, ratio, is_put),
            (float, float, float, float, float, float, bool),
        )
        return _rust.implied_vol_vec(p, s, k, t, rr, rt, ip).reshape(shape)

    def bs_delta_vec(S, K, T, r, sigma, ratio, is_put):
        (s, k, t, rr, sg, rt, ip), shape = _flat(
            (S, K, T, r, sigma, ratio, is_put),
            (float, float, float, float, float, float, bool),
        )
        return _rust.bs_delta_vec(s, k, t, rr, sg, rt, ip).reshape(shape)

    def _refine_iv_for_rounding(iv, price, S, K, T, r, ratio, is_put,
                                check_delta=False, check_leverage=False,
                                lev_price=None, eps=5e-6):
        iv = np.ascontiguousarray(np.asarray(iv, dtype=float).ravel())
        n = iv.shape[0]
        if n == 0:
            return iv
        lev = price if lev_price is None else lev_price

        def _b(v, dtype=float):
            return np.ascontiguousarray(
                np.broadcast_to(np.asarray(v, dtype=dtype), (n,)), dtype=dtype
            )

        return _rust.refine_iv_for_rounding(
            iv, _b(price), _b(S), _b(K), _b(T), _b(r), _b(ratio), _b(is_put, bool),
            _b(lev), bool(check_delta), bool(check_leverage), float(eps),
        )

else:  # pragma: no cover - exercised only when the extension is missing
    bs_price = bs_python.bs_price
    bs_delta = bs_python.bs_delta
    bs_vega = bs_python.bs_vega
    implied_vol = bs_python.implied_vol
    implied_vol_vec = bs_python.implied_vol_vec
    bs_delta_vec = bs_python.bs_delta_vec
    _refine_iv_for_rounding = bs_python._refine_iv_for_rounding


__all__ = [
    "ENGINE", "RUST_AVAILABLE", "RUST_IMPORT_ERROR", "engine_info",
    "bs_price", "bs_delta", "bs_vega", "calc_real_leverage",
    "implied_vol", "implied_vol_vec", "bs_delta_vec", "_refine_iv_for_rounding",
]
