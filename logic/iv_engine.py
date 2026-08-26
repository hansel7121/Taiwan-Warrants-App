"""Engine registry: which implementation of each compute-heavy feature is live.

Two interchangeable engines sit behind every name here: the compiled Rust
extension ``warrants_core`` (used whenever it imports) and a pure-Python/NumPy
reference (the backup). Both produce columns that are identical after
``round(..., 4)`` — the Rust IV solver reuses the same ``[1e-6, 10]`` bracket,
the same certified-Newton acceptance test, and a port of SciPy's ``brentq`` for
the exact fallback — so switching engines cannot move a displayed number.

``RUST_ENGINE=rust|python|auto`` (default ``auto``) picks the backend at import
for every feature at once. ``rust`` is a hard requirement and raises if the
extension will not import; ``auto`` falls back silently, which is why
``/healthz`` and the boot log report what actually won.

``RUST_ENGINE_OFF=arb,warrant_frame`` disables named features individually while
leaving the rest on Rust — the escape hatch for a production incident, where
dropping every feature back to Python is a bigger change than the one at fault.

``IV_ENGINE`` is the previous name for ``RUST_ENGINE`` and still works.

Selection happens once, at import, by binding module-level names — never a
per-call ``if RUST:``. Callers get the winners re-exported from
``logic.warrant_logic``.
"""
import os

import numpy as np
import pandas as pd

from logic import arb_kernels_py
from logic import bs_python

# Always available regardless of engine: trivial, no solver involved.
calc_real_leverage = bs_python.calc_real_leverage

# Features that have both engines. Named so RUST_ENGINE_OFF can address them.
FEATURES = ("iv", "arb", "warrant_frame")

_MODE = (os.getenv("RUST_ENGINE") or os.getenv("IV_ENGINE") or "auto").strip().lower()
_OFF = {f.strip().lower() for f in (os.getenv("RUST_ENGINE_OFF") or "").split(",")
        if f.strip()}

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


def use_rust(feature):
    """Whether `feature` runs on Rust in this process."""
    return RUST_AVAILABLE and feature not in _OFF


def feature_engines():
    """{feature: "rust"|"python"} — what actually ran, not what was asked for."""
    return {f: ("rust" if use_rust(f) else "python") for f in FEATURES}


def engine_info():
    """One-line engine description for boot logs and /healthz."""
    if RUST_AVAILABLE:
        base = f"rust warrants_core {_rust.__version__}"
        off = sorted(_OFF & set(FEATURES))
        return f"{base} (off: {','.join(off)})" if off else base
    if RUST_IMPORT_ERROR:
        return f"python (warrants_core unavailable: {RUST_IMPORT_ERROR})"
    return "python (RUST_ENGINE=python)"


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

if RUST_AVAILABLE and use_rust("iv"):

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

    def solve_tick(S, K, ratio, is_put, bid, ask):
        """Live Warrant tab tick kernel — time-value columns only, no IV/delta/
        leverage. See `bs_python.solve_tick` / `rust/warrants_core/src/tick.rs`."""
        if _is_scalar(S, K, ratio, is_put, bid, ask):
            return _rust.solve_tick(_F(S), _F(K), _F(ratio), _B(is_put), _F(bid), _F(ask))
        return bs_python.solve_tick(S, K, ratio, is_put, bid, ask)

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
    solve_tick = bs_python.solve_tick


# ── arb kernels ─────────────────────────────────────────────────────────────

if RUST_AVAILABLE and use_rust("arb"):

    def butterfly_pairs(wing_k, wing_buy, wing_dte,
                        body_k, body_sell, body_dte, body_orig, is_call):
        """See `arb_kernels_py.butterfly_pairs`. Rust returns seven parallel
        arrays; they are zipped here so both engines hand back the same tuples."""
        cols = _rust.butterfly_pairs(
            np.ascontiguousarray(wing_k, dtype=float),
            np.ascontiguousarray(wing_buy, dtype=float),
            np.ascontiguousarray(wing_dte, dtype=np.int64),
            np.ascontiguousarray(body_k, dtype=float),
            np.ascontiguousarray(body_sell, dtype=float),
            np.ascontiguousarray(body_dte, dtype=np.int64),
            np.ascontiguousarray(body_orig, dtype=np.int64),
            bool(is_call),
        )
        a, b, body, credit, tail, worst, guaranteed = (c.tolist() for c in cols)
        return list(zip(a, b, body, credit, tail, worst, guaranteed))

    def direct_pairs(w_type, w_is_put, w_strike, w_dte, w_ratio, w_ask, w_bid,
                     o_type, o_strike, o_dte, o_bid, o_ask, o_bid_live, o_ask_live,
                     max_dte_diff, positive_loose):
        """See `arb_kernels_py.direct_pairs`."""
        return _rust.direct_pairs(
            list(w_type), list(w_is_put), list(w_strike), list(w_dte),
            list(w_ratio), list(w_ask), list(w_bid),
            list(o_type), list(o_strike), list(o_dte), list(o_bid), list(o_ask),
            list(o_bid_live), list(o_ask_live), int(max_dte_diff),
            bool(positive_loose),
        )

else:  # pragma: no cover - exercised under RUST_ENGINE=python
    butterfly_pairs = arb_kernels_py.butterfly_pairs
    direct_pairs = arb_kernels_py.direct_pairs


# ── warrant frame ───────────────────────────────────────────────────────────
# Imported lazily inside the binding: warrant_frame_py imports the IV kernels
# from this module, so a top-level import here would be circular.

if RUST_AVAILABLE and use_rust("warrant_frame"):

    def build_warrant_df(cmoney_results, compute_iv=True, keep_noniv=False,
                         allow_no_quote=False):
        """CMoney payloads -> the scanner frame, built in Rust.

        Rust hands back one array per column, so `pd.DataFrame(..., copy=False)`
        wraps them as blocks instead of pandas inferring types from a list of
        several hundred dicts. Column arrays rather than a finished frame or a
        JSON buffer because the Supabase snapshot path builds the same frame
        from Postgres and feeds the same filters — a richer return type would
        need a second implementation of everything downstream.
        """
        from logic import warrant_frame_py

        cols = _rust.build_warrant_columns(
            cmoney_results, bool(compute_iv), bool(keep_noniv),
            bool(allow_no_quote), warrant_frame_py.R_FREE_DEFAULT,
        )
        if not len(cols["warrant_code"]):
            return pd.DataFrame(columns=warrant_frame_py.COL_ORDER)
        return pd.DataFrame(cols, copy=False)[warrant_frame_py.COL_ORDER]

else:  # pragma: no cover - exercised under RUST_ENGINE=python

    def build_warrant_df(cmoney_results, compute_iv=True, keep_noniv=False,
                         allow_no_quote=False):
        from logic import warrant_frame_py

        return warrant_frame_py.build_warrant_df(
            cmoney_results, compute_iv=compute_iv, keep_noniv=keep_noniv,
            allow_no_quote=allow_no_quote)


__all__ = [
    "ENGINE", "RUST_AVAILABLE", "RUST_IMPORT_ERROR", "engine_info",
    "FEATURES", "use_rust", "feature_engines",
    "bs_price", "bs_delta", "bs_vega", "calc_real_leverage",
    "implied_vol", "implied_vol_vec", "bs_delta_vec", "_refine_iv_for_rounding",
    "solve_tick",
    "butterfly_pairs", "direct_pairs", "build_warrant_df",
]
