//! Black-Scholes kernels: price, delta, vega, and the implied-vol solvers.
//!
//! Numerically mirrors `logic/bs_python.py` so the Rust and Python engines agree
//! on every column after `round(..., 4)`: the same expression order, the same
//! `[1e-6, 10]` bracket, and a faithful port of SciPy's `brentq` for the exact
//! fallback path.

/// SciPy's `brentq` default `rtol` (`4 * np.finfo(float).eps`).
pub const BRENT_RTOL: f64 = 8.881_784_197_001_252e-16;
pub const IV_LO: f64 = 1e-6;
pub const IV_HI: f64 = 10.0;

const FRAC_1_SQRT_2: f64 = std::f64::consts::FRAC_1_SQRT_2;
const INV_SQRT_2PI: f64 = 0.398_942_280_401_432_7;

/// Standard normal CDF; matches `scipy.stats.norm.cdf` to within an ulp.
#[inline(always)]
pub fn norm_cdf(x: f64) -> f64 {
    0.5 * libm::erfc(-x * FRAC_1_SQRT_2)
}

/// Standard normal PDF.
#[inline(always)]
pub fn norm_pdf(x: f64) -> f64 {
    INV_SQRT_2PI * (-0.5 * x * x).exp()
}

/// NumPy's `np.round(x, 4)` (scale, round-half-to-even, unscale).
#[inline(always)]
pub fn round4(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    (x * 1e4).round_ties_even() / 1e4
}

/// Black-Scholes price scaled by the exercise ratio; 0.0 for expired/zero-vol.
#[inline(always)]
pub fn bs_price(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64, is_put: bool) -> f64 {
    if t <= 0.0 || sigma <= 0.0 {
        return 0.0;
    }
    let sqrt_t = t.sqrt();
    let d1 = ((s / k).ln() + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t);
    let d2 = d1 - sigma * sqrt_t;
    let disc = (-r * t).exp();
    if is_put {
        ratio * (k * disc * norm_cdf(-d2) - s * norm_cdf(-d1))
    } else {
        ratio * (s * norm_cdf(d1) - k * disc * norm_cdf(d2))
    }
}

/// Black-Scholes delta scaled by the exercise ratio; 0.0 for expired/zero-vol.
#[inline(always)]
pub fn bs_delta(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64, is_put: bool) -> f64 {
    if t <= 0.0 || sigma <= 0.0 {
        return 0.0;
    }
    let d1 = ((s / k).ln() + (r + 0.5 * sigma * sigma) * t) / (sigma * t.sqrt());
    if is_put {
        (norm_cdf(d1) - 1.0) * ratio
    } else {
        norm_cdf(d1) * ratio
    }
}

/// Black-Scholes vega per 1.00 of sigma, scaled by the exercise ratio.
#[inline(always)]
pub fn bs_vega(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64) -> f64 {
    if t <= 0.0 || sigma <= 0.0 {
        return 0.0;
    }
    let sqrt_t = t.sqrt();
    let d1 = ((s / k).ln() + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t);
    s * norm_pdf(d1) * sqrt_t * ratio
}

/// Faithful port of SciPy's `brentq` (`scipy/optimize/Zeros/brentq.c`).
/// Returns NaN where SciPy raises (non-finite or same-sign bracket endpoints),
/// which is what the Python solver's `except: return nan` turns those into.
pub fn brentq<F: FnMut(f64) -> f64>(
    mut f: F,
    xa: f64,
    xb: f64,
    xtol: f64,
    rtol: f64,
    maxiter: usize,
) -> f64 {
    let mut xpre = xa;
    let mut xcur = xb;
    let mut xblk = 0.0f64;
    let mut fblk = 0.0f64;
    let mut spre = 0.0f64;
    let mut scur = 0.0f64;

    let mut fpre = f(xpre);
    let mut fcur = f(xcur);
    if !fpre.is_finite() || !fcur.is_finite() {
        return f64::NAN;
    }
    if fpre == 0.0 {
        return xpre;
    }
    if fcur == 0.0 {
        return xcur;
    }
    if fpre.is_sign_negative() == fcur.is_sign_negative() {
        return f64::NAN;
    }

    for _ in 0..maxiter {
        if fpre != 0.0 && fcur != 0.0 && (fpre.is_sign_negative() != fcur.is_sign_negative()) {
            xblk = xpre;
            fblk = fpre;
            spre = xcur - xpre;
            scur = spre;
        }
        if fblk.abs() < fcur.abs() {
            xpre = xcur;
            xcur = xblk;
            xblk = xpre;
            fpre = fcur;
            fcur = fblk;
            fblk = fpre;
        }

        let delta = (xtol + rtol * xcur.abs()) / 2.0;
        let sbis = (xblk - xcur) / 2.0;
        if fcur == 0.0 || sbis.abs() < delta {
            return xcur;
        }

        if spre.abs() > delta && fcur.abs() < fpre.abs() {
            let stry = if xpre == xblk {
                // interpolate
                -fcur * (xcur - xpre) / (fcur - fpre)
            } else {
                // extrapolate
                let dpre = (fpre - fcur) / (xpre - xcur);
                let dblk = (fblk - fcur) / (xblk - xcur);
                -fcur * (fblk * dblk - fpre * dpre) / (dblk * dpre * (fblk - fpre))
            };
            if 2.0 * stry.abs() < f64::min(spre.abs(), 3.0 * sbis.abs() - delta) {
                spre = scur;
                scur = stry;
            } else {
                spre = sbis;
                scur = sbis;
            }
        } else {
            spre = sbis;
            scur = sbis;
        }

        xpre = xcur;
        fpre = fcur;
        if scur.abs() > delta {
            xcur += scur;
        } else {
            xcur += if sbis > 0.0 { delta } else { -delta };
        }
        fcur = f(xcur);
    }
    xcur
}

/// Exact scalar IV: the `brentq` path, matching `bs_python.implied_vol`.
pub fn implied_vol(price: f64, s: f64, k: f64, t: f64, r: f64, ratio: f64, is_put: bool) -> f64 {
    if !(price > 0.0) || !(t > 0.0) {
        return f64::NAN;
    }
    let intrinsic = if is_put {
        f64::max(0.0, (k - s) * ratio)
    } else {
        f64::max(0.0, (s - k) * ratio)
    };
    if price <= intrinsic {
        return f64::NAN;
    }
    brentq(
        |sigma| bs_price(s, k, t, r, sigma, ratio, is_put) - price,
        IV_LO,
        IV_HI,
        1e-6,
        BRENT_RTOL,
        200,
    )
}

/// Fast scalar IV: bracket-safeguarded Newton seeded by Manaster-Koehler,
/// falling back to `implied_vol` (brentq) on any row it cannot certify.
///
/// The BS price is strictly increasing in sigma, so the root in `[1e-6, 10]` is
/// unique — a certified Newton root and brentq's root are the same root, within
/// brentq's `xtol`. Certification mirrors the Python fast path exactly: tight
/// price residual, well-conditioned vega, and sigma strictly inside the bracket.
pub fn implied_vol_fast(price: f64, s: f64, k: f64, t: f64, r: f64, ratio: f64, is_put: bool) -> f64 {
    if !(price > 0.0) || !(t > 0.0) {
        return f64::NAN;
    }
    let intrinsic = if is_put {
        f64::max(0.0, (k - s) * ratio)
    } else {
        f64::max(0.0, (s - k) * ratio)
    };
    if price <= intrinsic {
        return f64::NAN;
    }
    if !(s > 0.0) || !(k > 0.0) || !ratio.is_finite() {
        return implied_vol(price, s, k, t, r, ratio, is_put);
    }

    let sqrt_t = t.sqrt();
    let log_sk = (s / k).ln();
    let disc = (-r * t).exp();
    // model price and vega at one sigma, sharing d1/d2 between them
    let model = |sig: f64| -> (f64, f64, f64) {
        let d1 = (log_sk + (r + 0.5 * sig * sig) * t) / (sig * sqrt_t);
        let d2 = d1 - sig * sqrt_t;
        let m = if is_put {
            ratio * (k * disc * norm_cdf(-d2) - s * norm_cdf(-d1))
        } else {
            ratio * (s * norm_cdf(d1) - k * disc * norm_cdf(d2))
        };
        let vega = ratio * s * sqrt_t * norm_pdf(d1);
        (m, vega, d1)
    };

    // Manaster-Koehler seed: exact for at-the-money, close elsewhere.
    let mut sig = {
        let seed = (2.0 * (log_sk + r * t).abs() / t).sqrt();
        if seed.is_finite() && seed > 0.01 && seed < 5.0 {
            seed
        } else {
            0.5
        }
    };

    let tol = 1e-12 * price.abs().max(1.0);
    let mut lo = IV_LO;
    let mut hi = IV_HI;
    let mut converged = false;
    for _ in 0..64 {
        let (m, vega, _) = model(sig);
        let diff = m - price;
        if !diff.is_finite() {
            break;
        }
        // BS price rises with sigma, so the sign of the residual brackets the root.
        if diff > 0.0 {
            hi = sig;
        } else {
            lo = sig;
        }
        if diff.abs() <= tol {
            converged = true;
            break;
        }
        let mut next = sig - diff / vega;
        if !next.is_finite() || next <= lo || next >= hi {
            next = 0.5 * (lo + hi);
        }
        if next == sig {
            converged = true;
            break;
        }
        sig = next;
    }

    let (m_f, vega_f, _) = model(sig);
    let resid = (m_f - price).abs();
    // Scale-free vega = sqrt(T)*pdf(d1); ~1e-9 in the flat deep-ITM/OTM bands
    // where the price barely moves with sigma and the root is ill-conditioned.
    let norm_vega = (vega_f / (ratio * s)).abs();
    let accept = converged && resid < 1e-9 && norm_vega > 1e-6 && sig > IV_LO && sig < IV_HI;
    if accept {
        sig
    } else {
        implied_vol(price, s, k, t, r, ratio, is_put)
    }
}

/// One row of `_refine_iv_for_rounding`: re-solve with brentq when the rounded
/// IV / delta / leverage could flip anywhere in `[iv - eps, iv + eps]`.
#[allow(clippy::too_many_arguments)]
pub fn refine_row(
    iv: f64,
    price: f64,
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    ratio: f64,
    is_put: bool,
    check_delta: bool,
    check_leverage: bool,
    lev_price: f64,
    eps: f64,
) -> f64 {
    if iv.is_nan() {
        return iv;
    }
    let lo = (iv - eps).clamp(IV_LO, IV_HI);
    let hi = (iv + eps).clamp(IV_LO, IV_HI);
    let mut unstable = round4(lo) != round4(hi);
    if !unstable && (check_delta || check_leverage) {
        let d_lo = bs_delta(s, k, t, r, lo, ratio, is_put);
        let d_hi = bs_delta(s, k, t, r, hi, ratio, is_put);
        if check_delta {
            unstable |= round4(d_lo) != round4(d_hi);
        }
        if !unstable && check_leverage {
            let lev_lo = s * d_lo.abs() / lev_price;
            let lev_hi = s * d_hi.abs() / lev_price;
            unstable |= round4(lev_lo) != round4(lev_hi);
        }
    }
    if unstable {
        implied_vol(price, s, k, t, r, ratio, is_put)
    } else {
        iv
    }
}
