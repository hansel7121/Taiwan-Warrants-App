//! Python bindings for the Black-Scholes IV/delta kernels.
//!
//! Exposes the same call surface as `logic/bs_python.py` so `logic/iv_engine.py`
//! can swap one for the other. Every `*_vec` entry point takes equal-length,
//! C-contiguous 1-D arrays (the Python side broadcasts), releases the GIL, and
//! fans out over rayon above `PAR_MIN` rows.

mod bs;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// Row count above which the solve is worth handing to rayon.
const PAR_MIN: usize = 256;

fn same_len(n: usize, got: usize, name: &str) -> PyResult<()> {
    if n != got {
        return Err(PyValueError::new_err(format!(
            "length mismatch: expected {n} elements, `{name}` has {got}"
        )));
    }
    Ok(())
}

/// Run `f` over `0..n`, in parallel when the batch is big enough to pay for it.
fn map_rows<F>(n: usize, f: F) -> Vec<f64>
where
    F: Fn(usize) -> f64 + Send + Sync,
{
    if n >= PAR_MIN {
        (0..n).into_par_iter().map(f).collect()
    } else {
        (0..n).map(f).collect()
    }
}

#[pyfunction]
#[pyo3(signature = (s, k, t, r, sigma, ratio, is_put=false))]
fn bs_price(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64, is_put: bool) -> f64 {
    bs::bs_price(s, k, t, r, sigma, ratio, is_put)
}

#[pyfunction]
#[pyo3(signature = (s, k, t, r, sigma, ratio, is_put=false))]
fn bs_delta(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64, is_put: bool) -> f64 {
    bs::bs_delta(s, k, t, r, sigma, ratio, is_put)
}

#[pyfunction]
fn bs_vega(s: f64, k: f64, t: f64, r: f64, sigma: f64, ratio: f64) -> f64 {
    bs::bs_vega(s, k, t, r, sigma, ratio)
}

/// Exact scalar IV (brentq), matching `bs_python.implied_vol` bit-for-bit.
#[pyfunction]
#[pyo3(signature = (price, s, k, t, r, ratio, is_put=false))]
fn implied_vol(price: f64, s: f64, k: f64, t: f64, r: f64, ratio: f64, is_put: bool) -> f64 {
    bs::implied_vol(price, s, k, t, r, ratio, is_put)
}

/// Certified-Newton scalar IV with a brentq fallback — the fast path.
#[pyfunction]
#[pyo3(signature = (price, s, k, t, r, ratio, is_put=false))]
fn implied_vol_fast(price: f64, s: f64, k: f64, t: f64, r: f64, ratio: f64, is_put: bool) -> f64 {
    bs::implied_vol_fast(price, s, k, t, r, ratio, is_put)
}

/// Vectorized IV over equal-length contiguous arrays.
#[pyfunction]
fn implied_vol_vec<'py>(
    py: Python<'py>,
    price: PyReadonlyArray1<'py, f64>,
    s: PyReadonlyArray1<'py, f64>,
    k: PyReadonlyArray1<'py, f64>,
    t: PyReadonlyArray1<'py, f64>,
    r: PyReadonlyArray1<'py, f64>,
    ratio: PyReadonlyArray1<'py, f64>,
    is_put: PyReadonlyArray1<'py, bool>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let price = price.as_slice()?;
    let n = price.len();
    let s = s.as_slice()?;
    let k = k.as_slice()?;
    let t = t.as_slice()?;
    let r = r.as_slice()?;
    let ratio = ratio.as_slice()?;
    let is_put = is_put.as_slice()?;
    same_len(n, s.len(), "S")?;
    same_len(n, k.len(), "K")?;
    same_len(n, t.len(), "T")?;
    same_len(n, r.len(), "r")?;
    same_len(n, ratio.len(), "ratio")?;
    same_len(n, is_put.len(), "is_put")?;

    // Same shape as the Python fast path: certified Newton, then the standard
    // rounding-edge refinement so the result rounds like the brentq solver.
    let out = py.allow_threads(|| {
        map_rows(n, |i| {
            let v = bs::implied_vol_fast(price[i], s[i], k[i], t[i], r[i], ratio[i], is_put[i]);
            bs::refine_row(
                v, price[i], s[i], k[i], t[i], r[i], ratio[i], is_put[i],
                false, false, price[i], 5e-6,
            )
        })
    });
    Ok(out.into_pyarray_bound(py))
}

/// Vectorized exact IV (brentq on every row) — the parity reference.
#[pyfunction]
fn implied_vol_vec_exact<'py>(
    py: Python<'py>,
    price: PyReadonlyArray1<'py, f64>,
    s: PyReadonlyArray1<'py, f64>,
    k: PyReadonlyArray1<'py, f64>,
    t: PyReadonlyArray1<'py, f64>,
    r: PyReadonlyArray1<'py, f64>,
    ratio: PyReadonlyArray1<'py, f64>,
    is_put: PyReadonlyArray1<'py, bool>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let price = price.as_slice()?;
    let n = price.len();
    let s = s.as_slice()?;
    let k = k.as_slice()?;
    let t = t.as_slice()?;
    let r = r.as_slice()?;
    let ratio = ratio.as_slice()?;
    let is_put = is_put.as_slice()?;
    same_len(n, s.len(), "S")?;
    same_len(n, k.len(), "K")?;
    same_len(n, t.len(), "T")?;
    same_len(n, r.len(), "r")?;
    same_len(n, ratio.len(), "ratio")?;
    same_len(n, is_put.len(), "is_put")?;

    let out = py.allow_threads(|| {
        map_rows(n, |i| {
            bs::implied_vol(price[i], s[i], k[i], t[i], r[i], ratio[i], is_put[i])
        })
    });
    Ok(out.into_pyarray_bound(py))
}

/// Vectorized delta over equal-length contiguous arrays.
#[pyfunction]
fn bs_delta_vec<'py>(
    py: Python<'py>,
    s: PyReadonlyArray1<'py, f64>,
    k: PyReadonlyArray1<'py, f64>,
    t: PyReadonlyArray1<'py, f64>,
    r: PyReadonlyArray1<'py, f64>,
    sigma: PyReadonlyArray1<'py, f64>,
    ratio: PyReadonlyArray1<'py, f64>,
    is_put: PyReadonlyArray1<'py, bool>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let s = s.as_slice()?;
    let n = s.len();
    let k = k.as_slice()?;
    let t = t.as_slice()?;
    let r = r.as_slice()?;
    let sigma = sigma.as_slice()?;
    let ratio = ratio.as_slice()?;
    let is_put = is_put.as_slice()?;
    same_len(n, k.len(), "K")?;
    same_len(n, t.len(), "T")?;
    same_len(n, r.len(), "r")?;
    same_len(n, sigma.len(), "sigma")?;
    same_len(n, ratio.len(), "ratio")?;
    same_len(n, is_put.len(), "is_put")?;

    let out = py.allow_threads(|| {
        map_rows(n, |i| {
            bs::bs_delta(s[i], k[i], t[i], r[i], sigma[i], ratio[i], is_put[i])
        })
    });
    Ok(out.into_pyarray_bound(py))
}

/// Vectorized rounding-edge refinement: re-solve with brentq only the rows whose
/// rounded IV/delta/leverage could flip within `[iv - eps, iv + eps]`.
#[pyfunction]
#[pyo3(signature = (iv, price, s, k, t, r, ratio, is_put, lev_price,
                    check_delta=false, check_leverage=false, eps=5e-6))]
#[allow(clippy::too_many_arguments)]
fn refine_iv_for_rounding<'py>(
    py: Python<'py>,
    iv: PyReadonlyArray1<'py, f64>,
    price: PyReadonlyArray1<'py, f64>,
    s: PyReadonlyArray1<'py, f64>,
    k: PyReadonlyArray1<'py, f64>,
    t: PyReadonlyArray1<'py, f64>,
    r: PyReadonlyArray1<'py, f64>,
    ratio: PyReadonlyArray1<'py, f64>,
    is_put: PyReadonlyArray1<'py, bool>,
    lev_price: PyReadonlyArray1<'py, f64>,
    check_delta: bool,
    check_leverage: bool,
    eps: f64,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let iv = iv.as_slice()?;
    let n = iv.len();
    let price = price.as_slice()?;
    let s = s.as_slice()?;
    let k = k.as_slice()?;
    let t = t.as_slice()?;
    let r = r.as_slice()?;
    let ratio = ratio.as_slice()?;
    let is_put = is_put.as_slice()?;
    let lev_price = lev_price.as_slice()?;
    same_len(n, price.len(), "price")?;
    same_len(n, s.len(), "S")?;
    same_len(n, k.len(), "K")?;
    same_len(n, t.len(), "T")?;
    same_len(n, r.len(), "r")?;
    same_len(n, ratio.len(), "ratio")?;
    same_len(n, is_put.len(), "is_put")?;
    same_len(n, lev_price.len(), "lev_price")?;

    let out = py.allow_threads(|| {
        map_rows(n, |i| {
            bs::refine_row(
                iv[i], price[i], s[i], k[i], t[i], r[i], ratio[i], is_put[i],
                check_delta, check_leverage, lev_price[i], eps,
            )
        })
    });
    Ok(out.into_pyarray_bound(py))
}

#[pymodule]
fn warrants_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(bs_price, m)?)?;
    m.add_function(wrap_pyfunction!(bs_delta, m)?)?;
    m.add_function(wrap_pyfunction!(bs_vega, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol_fast, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol_vec, m)?)?;
    m.add_function(wrap_pyfunction!(implied_vol_vec_exact, m)?)?;
    m.add_function(wrap_pyfunction!(bs_delta_vec, m)?)?;
    m.add_function(wrap_pyfunction!(refine_iv_for_rounding, m)?)?;
    Ok(())
}
