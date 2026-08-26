//! Python bindings for the Black-Scholes IV/delta kernels.
//!
//! Exposes the same call surface as `logic/bs_python.py` so `logic/iv_engine.py`
//! can swap one for the other. Every `*_vec` entry point takes equal-length,
//! C-contiguous 1-D arrays (the Python side broadcasts), releases the GIL, and
//! fans out over rayon above `PAR_MIN` rows.

mod arb;
mod bs;
mod frame;
mod tick;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;

/// Row count above which the solve is worth handing to rayon.
pub(crate) const PAR_MIN: usize = 256;

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

/// Every wing pair whose best body yields a locked-in credit.
///
/// Wings must arrive sorted by strike and bodies sorted by strike with their
/// original chain positions in `body_orig` — see `arb::butterfly_pairs`.
/// Returns seven parallel arrays: the two wing positions, the chosen body's
/// original position, and the four economics values, already rounded the way
/// the Python matcher rounds them.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn butterfly_pairs<'py>(
    py: Python<'py>,
    wing_k: PyReadonlyArray1<'py, f64>,
    wing_buy: PyReadonlyArray1<'py, f64>,
    wing_dte: PyReadonlyArray1<'py, i64>,
    body_k: PyReadonlyArray1<'py, f64>,
    body_sell: PyReadonlyArray1<'py, f64>,
    body_dte: PyReadonlyArray1<'py, i64>,
    body_orig: PyReadonlyArray1<'py, i64>,
    is_call: bool,
) -> PyResult<(
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
)> {
    let wk = wing_k.as_slice()?;
    let nw = wk.len();
    let wb = wing_buy.as_slice()?;
    let wd = wing_dte.as_slice()?;
    same_len(nw, wb.len(), "wing_buy")?;
    same_len(nw, wd.len(), "wing_dte")?;
    let bk = body_k.as_slice()?;
    let nb = bk.len();
    let bs_ = body_sell.as_slice()?;
    let bd = body_dte.as_slice()?;
    let bo = body_orig.as_slice()?;
    same_len(nb, bs_.len(), "body_sell")?;
    same_len(nb, bd.len(), "body_dte")?;
    same_len(nb, bo.len(), "body_orig")?;

    let hits = py.allow_threads(|| arb::butterfly_pairs(wk, wb, wd, bk, bs_, bd, bo, is_call));
    let n = hits.len();
    let mut a = Vec::with_capacity(n);
    let mut b = Vec::with_capacity(n);
    let mut body = Vec::with_capacity(n);
    let mut credit = Vec::with_capacity(n);
    let mut tail = Vec::with_capacity(n);
    let mut worst = Vec::with_capacity(n);
    let mut guaranteed = Vec::with_capacity(n);
    for h in hits {
        a.push(h.wing_lo);
        b.push(h.wing_hi);
        body.push(h.body);
        credit.push(h.credit_ps);
        tail.push(h.tail);
        worst.push(h.worst_payoff_ps);
        guaranteed.push(h.guaranteed_ps);
    }
    Ok((
        a.into_pyarray_bound(py),
        b.into_pyarray_bound(py),
        body.into_pyarray_bound(py),
        credit.into_pyarray_bound(py),
        tail.into_pyarray_bound(py),
        worst.into_pyarray_bound(py),
        guaranteed.into_pyarray_bound(py),
    ))
}

/// The warrant scanner's whole compute stage: CMoney payloads in, DataFrame
/// columns out. See `frame.rs`; `logic/warrant_logic.py` wraps the result in
/// `pd.DataFrame(cols, copy=False)`.
#[pyfunction]
#[pyo3(signature = (results, compute_iv=true, keep_noniv=false, allow_no_quote=false, r_free=0.02))]
fn build_warrant_columns<'py>(
    py: Python<'py>,
    results: &Bound<'py, PyDict>,
    compute_iv: bool,
    keep_noniv: bool,
    allow_no_quote: bool,
    r_free: f64,
) -> PyResult<Bound<'py, PyDict>> {
    let rows = frame::parse_all(py, results, allow_no_quote)?;
    let cols = py.allow_threads(move || frame::solve(rows, compute_iv, keep_noniv, r_free));
    frame::to_columns(py, &cols)
}

/// Every pair the same-type warrant/option matcher would emit, in emission
/// order. See `arb::direct_pairs`; the caller dedups and builds the rows.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn direct_pairs<'py>(
    py: Python<'py>,
    w_type: Vec<i64>, w_is_put: Vec<bool>, w_strike: Vec<f64>, w_dte: Vec<i64>,
    w_ratio: Vec<f64>, w_ask: Vec<f64>, w_bid: Vec<f64>,
    o_type: Vec<i64>, o_strike: Vec<f64>, o_dte: Vec<i64>, o_bid: Vec<f64>,
    o_ask: Vec<f64>, o_bid_live: Vec<bool>, o_ask_live: Vec<bool>,
    max_dte_diff: i64, positive_loose: bool,
) -> PyResult<Vec<(i64, i64, i64, f64, f64, f64, f64, i64, bool, f64)>> {
    let nw = w_strike.len();
    same_len(nw, w_type.len(), "w_type")?;
    same_len(nw, w_is_put.len(), "w_is_put")?;
    same_len(nw, w_dte.len(), "w_dte")?;
    same_len(nw, w_ratio.len(), "w_ratio")?;
    same_len(nw, w_ask.len(), "w_ask")?;
    same_len(nw, w_bid.len(), "w_bid")?;
    let no = o_strike.len();
    same_len(no, o_type.len(), "o_type")?;
    same_len(no, o_dte.len(), "o_dte")?;
    same_len(no, o_bid.len(), "o_bid")?;
    same_len(no, o_ask.len(), "o_ask")?;
    same_len(no, o_bid_live.len(), "o_bid_live")?;
    same_len(no, o_ask_live.len(), "o_ask_live")?;

    let hits = py.allow_threads(|| {
        arb::direct_pairs(&w_type, &w_is_put, &w_strike, &w_dte, &w_ratio, &w_ask,
                          &w_bid, &o_type, &o_strike, &o_dte, &o_bid, &o_ask,
                          &o_bid_live, &o_ask_live, max_dte_diff, positive_loose)
    });
    Ok(hits.into_iter()
        .map(|h| (h.wi, h.oi, h.direction, h.price_diff, h.exec_opt, h.exec_warrant,
                  h.strike_diff_pct, h.dte_diff, h.favorable, h.max_loss_per_share))
        .collect())
}

/// Python's builtin `round(x, nd)`, exposed so a test can prove the Rust and
/// CPython roundings agree before any kernel relies on it.
#[pyfunction]
fn round_py(x: f64, nd: usize) -> f64 {
    arb::round_py(x, nd)
}

/// Live Warrant tab tick kernel: time-value columns only for one warrant's
/// current best bid/ask. No IV/delta/leverage solve — see `tick.rs`.
#[pyfunction]
fn solve_tick(s: f64, k: f64, ratio: f64, is_put: bool, bid: f64, ask: f64) -> (f64, f64, f64) {
    let r = tick::solve_tick(s, k, ratio, is_put, bid, ask);
    (r.time_value, r.bid_time_value_pct, r.ask_time_value_pct)
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
    m.add_function(wrap_pyfunction!(butterfly_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(direct_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(round_py, m)?)?;
    m.add_function(wrap_pyfunction!(build_warrant_columns, m)?)?;
    m.add_function(wrap_pyfunction!(solve_tick, m)?)?;
    Ok(())
}
