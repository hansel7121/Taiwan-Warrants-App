"""Pure-Python/NumPy Black-Scholes reference: price, delta, vega, IV (Brent's
method) and their vectorized Newton fast paths.

This is the BACKUP / reference engine. `logic/iv_engine.py` picks between this
module and the compiled Rust engine (`warrants_core`) and re-exports the winner;
callers import from `logic.warrant_logic`, never from here directly. Kept intact
so the app still runs — and so parity tests still have something to compare
against — when the Rust extension is unavailable.
"""
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq


def bs_price(S, K, T, r, sigma, ratio, is_put=False):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if is_put:
        return ratio * (K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1))
    return ratio * (S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def bs_delta(S, K, T, r, sigma, ratio, is_put=False):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    if is_put:
        return (norm.cdf(d1) - 1) * ratio
    return norm.cdf(d1) * ratio


def bs_vega(S, K, T, r, sigma, ratio):
    """Black-Scholes vega, per 1.00 (=100 vol-points) change in sigma, scaled by
    the instrument's exercise ratio. Mirrors the frontend `bsVega`. Vega does not
    depend on call/put, so no is_put arg (unlike bs_price / bs_delta)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    return S * norm.pdf(d1) * np.sqrt(T) * ratio


def calc_real_leverage(S, delta, ask):
    if ask <= 0:
        return 0.0
    return S * delta / ask


def _pct_of_spot(value, S):
    """`value` as a % of spot; NaN in, NaN out. Mirrors `warrant_frame_py._pct_of_spot`."""
    if value is None or not np.isfinite(value):
        return np.nan
    return round(value / S * 100, 4)


def solve_tick(S, K, ratio, is_put, bid, ask):
    """Live Warrant tab tick kernel: time-value columns only, for one warrant's
    current best bid/ask. Deliberately does NOT solve IV, delta or leverage —
    those are never computed for this feature, not even as an unexposed
    intermediate — so this needs no T/r input at all. Mirrors the time-value
    block of `warrant_frame_py.build_warrant_df` (and Rust's `tick.rs`) exactly,
    minus `intrinsic` (only `time_value_am` needed it, and that column is
    dropped). Returns (time_value, bid_time_value_pct, ask_time_value_pct).
    """
    distance_to_strike = (S - K) if is_put else (K - S)

    # A live tick must never raise on a bad ratio — degrade that one field to
    # NaN (rendered "—") rather than crash the tick pipeline.
    def _div(x, y):
        return x / y if y != 0 else np.nan

    time_value = _div(ask, ratio) + distance_to_strike if ask > 0 else np.nan
    bid_time_value = _div(bid, ratio) + distance_to_strike if bid > 0 else np.nan

    return (
        round(time_value, 4) if np.isfinite(time_value) else np.nan,
        _pct_of_spot(bid_time_value, S),
        _pct_of_spot(time_value, S),
    )


def implied_vol(price, S, K, T, r, ratio, is_put=False):
    if price <= 0 or T <= 0:
        return np.nan
    intrinsic = max(0, (K - S) * ratio) if is_put else max(0, (S - K) * ratio)
    if price <= intrinsic:
        return np.nan
    try:
        return brentq(
            lambda sigma: bs_price(S, K, T, r, sigma, ratio, is_put) - price,
            1e-6,
            10.0,
            xtol=1e-6,
            maxiter=200,
        )
    except Exception:
        return np.nan


def implied_vol_vec(price, S, K, T, r, ratio, is_put):
    """Vectorized implied_vol. Mirrors the scalar ``implied_vol`` exactly (after
    round(...,4)): NaN where price<=0, T<=0, or price<=intrinsic; otherwise the
    Black-Scholes IV in [1e-6, 10].

    All args are numpy arrays (or broadcastable). The easy 99% are solved by a
    single array Newton-Raphson sweep; any row that fails to converge, hits a
    bound, or whose residual is not tight enough falls back to the scalar
    ``implied_vol`` (brentq) in a short loop — so the hard cases agree with the
    scalar solver bit-for-bit and the common cases go ~10x+ faster.
    """
    price = np.asarray(price, dtype=float)
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    r = np.asarray(r, dtype=float)
    ratio = np.asarray(ratio, dtype=float)
    is_put = np.asarray(is_put, dtype=bool)
    price, S, K, T, r, ratio, is_put = np.broadcast_arrays(
        price, S, K, T, r, ratio, is_put
    )
    shape = price.shape
    out = np.full(shape, np.nan)
    pf = np.ascontiguousarray(price, dtype=float).ravel()
    Sf = np.ascontiguousarray(S, dtype=float).ravel()
    Kf = np.ascontiguousarray(K, dtype=float).ravel()
    Tf = np.ascontiguousarray(T, dtype=float).ravel()
    rf = np.ascontiguousarray(r, dtype=float).ravel()
    ratf = np.ascontiguousarray(ratio, dtype=float).ravel()
    ipf = np.ascontiguousarray(is_put, dtype=bool).ravel()

    # intrinsic matches the scalar exactly: put=max(0,(K-S)*ratio),
    # call=max(0,(S-K)*ratio). NaN unless price>0, T>0 and price>intrinsic.
    with np.errstate(all="ignore"):
        intrinsic = np.where(
            ipf,
            np.maximum(0.0, (Kf - Sf) * ratf),
            np.maximum(0.0, (Sf - Kf) * ratf),
        )
        valid = (pf > 0) & (Tf > 0) & (pf > intrinsic)
    idx = np.where(valid)[0]
    if idx.size == 0:
        return out

    p = pf[idx]; s = Sf[idx]; k = Kf[idx]; t = Tf[idx]
    rr = rf[idx]; rt = ratf[idx]; ip = ipf[idx]
    sqrtT = np.sqrt(t)
    logSK = np.log(s / k)

    def _model_and_vega(sig):
        d1 = (logSK + (rr + 0.5 * sig * sig) * t) / (sig * sqrtT)
        d2 = d1 - sig * sqrtT
        disc = np.exp(-rr * t)
        call = rt * (s * norm.cdf(d1) - k * disc * norm.cdf(d2))
        put = rt * (k * disc * norm.cdf(-d2) - s * norm.cdf(-d1))
        m = np.where(ip, put, call)
        vega = rt * s * sqrtT * norm.pdf(d1)
        return m, vega

    sig = np.full(idx.size, 0.5)
    with np.errstate(all="ignore"):
        for _ in range(50):
            sig = np.clip(sig, 1e-6, 10.0)
            m, vega = _model_and_vega(sig)
            diff = m - p
            if np.all(np.abs(diff) < 1e-10):
                break
            vega = np.where(np.abs(vega) < 1e-12, 1e-12, vega)
            sig = np.clip(sig - diff / vega, 1e-6, 10.0)
        m, vega_final = _model_and_vega(sig)
        resid = np.abs(m - p)
        # Scale-free vega: vega/(ratio*S) = sqrt(T)*pdf(d1). O(0.01-0.5) for a
        # well-conditioned root, ~1e-9 in flat deep-ITM/OTM bands where bs_price
        # is numerically insensitive to sigma over a wide range.
        norm_vega = np.abs(vega_final) / (rt * s)

    # Accept Newton only where it MUST agree with brentq after round(...,4):
    # tight price residual (< 1e-9), well-conditioned root (norm_vega > 1e-6,
    # else flat/degenerate regions can settle on a different root), and sigma
    # strictly inside the bracket. Everything else falls back to scalar brentq.
    accept = (
        (resid < 1e-9)
        & (norm_vega > 1e-6)
        & (sig > 1e-6)
        & (sig < 10.0)
    )
    res = np.full(idx.size, np.nan)
    res[accept] = sig[accept]
    for j in np.where(~accept)[0]:
        res[j] = implied_vol(
            float(p[j]), float(s[j]), float(k[j]), float(t[j]),
            float(rr[j]), float(rt[j]), bool(ip[j]),
        )
    # Rounding-edge safety for the IV column itself: any Newton-accepted row
    # whose round(...,4) could flip between Newton's ~exact root and brentq's
    # (root within ~1e-6 of a .00005 boundary) is re-solved with the scalar
    # brentq, so the returned array rounds bit-identically to the scalar solver.
    res = _refine_iv_for_rounding(res, p, s, k, t, rr, rt, ip)
    out.flat[idx] = res
    return out


def bs_delta_vec(S, K, T, r, sigma, ratio, is_put):
    """Vectorized bs_delta. Same semantics as the scalar: 0.0 where T<=0 or
    sigma<=0; NaN propagates where sigma is NaN with T>0 (matching the scalar,
    whose ``sigma<=0`` guard is False for NaN so it computes NaN)."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    r = np.asarray(r, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    ratio = np.asarray(ratio, dtype=float)
    is_put = np.asarray(is_put, dtype=bool)
    S, K, T, r, sigma, ratio, is_put = np.broadcast_arrays(
        S, K, T, r, sigma, ratio, is_put
    )
    zero_mask = (T <= 0) | (sigma <= 0)
    with np.errstate(all="ignore"):
        d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * np.sqrt(T))
        val = np.where(is_put, (norm.cdf(d1) - 1.0) * ratio, norm.cdf(d1) * ratio)
    return np.where(zero_mask, 0.0, val)


def _refine_iv_for_rounding(iv, price, S, K, T, r, ratio, is_put,
                            check_delta=False, check_leverage=False,
                            lev_price=None, eps=5e-6):
    """Guarantee bit-exact parity of the 4-dp-rounded IV/delta/leverage columns
    against the scalar brentq path.

    ``iv`` is a vectorized-Newton result (NaN where unconverged). Newton's root
    is ~machine-precise and brentq's is within ~1e-6 of the true root, so
    |iv_newton - iv_brentq| < eps. For any row whose rounded IV (and optionally
    delta / leverage, computed exactly as the call site does) is UNCHANGED as iv
    moves across [iv-eps, iv+eps], both solvers must yield identical rounded
    columns. Only rows sitting on a rounding edge (the output flips within that
    window) are recomputed with the exact scalar brentq, so they match the
    scalar path bit-for-bit. This keeps the fast Newton path for the ~98% of
    rounding-stable rows while removing every derived-column boundary flip.
    """
    iv = np.array(iv, dtype=float)
    n = iv.shape[0]
    price = np.broadcast_to(np.asarray(price, dtype=float), (n,))
    S = np.broadcast_to(np.asarray(S, dtype=float), (n,))
    K = np.broadcast_to(np.asarray(K, dtype=float), (n,))
    T = np.broadcast_to(np.asarray(T, dtype=float), (n,))
    r = np.broadcast_to(np.asarray(r, dtype=float), (n,))
    ratio = np.broadcast_to(np.asarray(ratio, dtype=float), (n,))
    is_put = np.broadcast_to(np.asarray(is_put, dtype=bool), (n,))
    lev_price = price if lev_price is None else np.broadcast_to(
        np.asarray(lev_price, dtype=float), (n,))

    finite = ~np.isnan(iv)
    if not finite.any():
        return iv
    with np.errstate(all="ignore"):
        lo = np.clip(iv - eps, 1e-6, 10.0)
        hi = np.clip(iv + eps, 1e-6, 10.0)
        unstable = np.round(lo, 4) != np.round(hi, 4)
        if check_delta or check_leverage:
            d_lo = bs_delta_vec(S, K, T, r, lo, ratio, is_put)
            d_hi = bs_delta_vec(S, K, T, r, hi, ratio, is_put)
            if check_delta:
                unstable |= np.round(d_lo, 4) != np.round(d_hi, 4)
            if check_leverage:
                lev_lo = S * np.abs(d_lo) / lev_price
                lev_hi = S * np.abs(d_hi) / lev_price
                unstable |= np.round(lev_lo, 4) != np.round(lev_hi, 4)
    unstable &= finite
    out = iv.copy()
    for j in np.where(unstable)[0]:
        out[j] = implied_vol(float(price[j]), float(S[j]), float(K[j]),
                             float(T[j]), float(r[j]), float(ratio[j]),
                             bool(is_put[j]))
    return out

