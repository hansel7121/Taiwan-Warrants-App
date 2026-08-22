//! Warrant-frame construction: CMoney payloads in, DataFrame columns out.
//!
//! Mirrors `logic/warrant_logic.py::build_warrant_df` — the same coercions, the
//! same drop guards, the same `round(x, 4)` (Python's, via `arb::round_py`), and
//! the same IV/delta/leverage sequence. Only the columns cross back into Python;
//! `pd.DataFrame(cols, copy=False)` then wraps them without copying, instead of
//! building 725 dicts and letting pandas infer.

use pyo3::prelude::*;
use pyo3::intern;
use pyo3::types::{PyDict, PyList, PyString};
use rayon::prelude::*;

use crate::arb::round_py;
use crate::bs;

/// The parse guards drop a row rather than failing the scan, matching the
/// Python parse loop's `except (KeyError, TypeError, ValueError, ...)`.
type RowResult<T> = Result<T, ()>;

/// Keys are interned once per call rather than rebuilt as Python strings on
/// every lookup — with ~14 lookups per warrant that was most of the parse.
fn item<'py>(d: &Bound<'py, PyDict>, key: &Bound<'py, PyString>) -> Option<Bound<'py, PyAny>> {
    d.get_item(key).ok().flatten()
}

/// `float(v or 0)`: absent, None and falsy all give 0.0; a numeric string is
/// parsed the way `float()` parses it; anything else drops the row.
fn f_or0(v: Option<Bound<'_, PyAny>>) -> RowResult<f64> {
    let Some(o) = v else { return Ok(0.0) };
    if o.is_none() {
        return Ok(0.0);
    }
    if let Ok(false) = o.is_truthy() {
        return Ok(0.0);
    }
    if let Ok(x) = o.extract::<f64>() {
        return Ok(x);
    }
    if let Ok(s) = o.extract::<String>() {
        return s.trim().parse::<f64>().map_err(|_| ());
    }
    Err(())
}

/// `int(v or 0)`: truncates a float toward zero, as `int()` does.
fn i_or0(v: Option<Bound<'_, PyAny>>) -> RowResult<i64> {
    let Some(o) = v else { return Ok(0) };
    if o.is_none() {
        return Ok(0);
    }
    if let Ok(false) = o.is_truthy() {
        return Ok(0);
    }
    if let Ok(x) = o.extract::<i64>() {
        return Ok(x);
    }
    if let Ok(x) = o.extract::<f64>() {
        return Ok(x.trunc() as i64);
    }
    if let Ok(s) = o.extract::<String>() {
        return s.trim().parse::<i64>().map_err(|_| ());
    }
    Err(())
}

fn as_str(v: Option<Bound<'_, PyAny>>, default: &str) -> String {
    match v {
        None => default.to_string(),
        Some(o) => o.str().map(|s| s.to_string()).unwrap_or_else(|_| default.to_string()),
    }
}

/// `x / y` where a zero divisor drops the row — Python raises ZeroDivisionError
/// on float division by zero and the parse loop catches it, so a warrant
/// quoting an exercise ratio of 0 has always been dropped here.
fn div(x: f64, y: f64) -> RowResult<f64> {
    if y == 0.0 {
        return Err(());
    }
    Ok(x / y)
}

/// `_pct_of_spot`: value as a % of spot, or NaN in / NaN out.
fn pct_of_spot(value: f64, s: f64) -> RowResult<f64> {
    if !value.is_finite() {
        return Ok(f64::NAN);
    }
    Ok(round_py(div(value, s)? * 100.0, 4))
}

/// One parsed warrant, before the IV solve.
pub struct Row {
    code: String,
    name: String,
    underlying_code: Option<String>,
    is_put: bool,
    underlying_price: f64,
    ask: f64,
    bid: f64,
    ask_qty: i64,
    bid_qty: i64,
    days_to_expiry: i64,
    strike: f64,
    exercise_ratio: f64,
    volume: i64,
    time_value: f64,
    bid_time_value_pct: f64,
    ask_time_value_pct: f64,
    time_value_am: f64,
}

pub(crate) fn parse_row<'py>(py: Python<'py>, code: &str, data: &Bound<'py, PyAny>,
                             allow_no_quote: bool) -> RowResult<Option<Row>> {
    let d = data.downcast::<PyDict>().map_err(|_| ())?;
    let w = item(d, intern!(py, "Warrant")).ok_or(())?;
    let s = item(d, intern!(py, "Stock")).ok_or(())?;
    let w = w.downcast::<PyDict>().map_err(|_| ())?;
    let s = s.downcast::<PyDict>().map_err(|_| ())?;

    let comm_key = item(s, intern!(py, "CommKey"));
    let underlying_code = match comm_key {
        Some(o) if !o.is_none() => Some(o.str().map_err(|_| ())?.to_string()),
        _ => None,
    };
    let underlying_price = f_or0(item(s, intern!(py, "SalePr")))?;
    let ask = f_or0(item(w, intern!(py, "SellPr1")))?;
    let bid = f_or0(item(w, intern!(py, "BuyPr1")))?;
    let ask_qty = i_or0(item(w, intern!(py, "SellQty1")))?;
    let bid_qty = i_or0(item(w, intern!(py, "BuyQty1")))?;
    let volume = i_or0(item(w, intern!(py, "SaleQty")))?;
    let name = as_str(item(w, intern!(py, "CommName")), "");
    let days_to_expiry = i_or0(item(w, intern!(py, "LastDays")))?;
    let strike = f_or0(item(w, intern!(py, "StrikePr")))?;
    let exercise_ratio = f_or0(item(w, intern!(py, "UserRate")))?;
    let is_put = {
        let v = item(w, intern!(py, "CallorPut"));
        let raw = match v {
            None => 1,
            Some(o) if o.is_none() => 1,
            Some(o) => {
                if let Ok(false) = o.is_truthy() {
                    1
                } else {
                    i_or0(Some(o))?
                }
            }
        };
        raw == 2
    };

    if underlying_price <= 0.0 || days_to_expiry <= 0 {
        return Ok(None);
    }
    // Every ask-side metric divides by the ask, so a warrant quoting no offer
    // has none of them.
    if ask <= 0.0 && !allow_no_quote {
        return Ok(None);
    }

    let (intrinsic, distance_to_strike) = if is_put {
        (f64::max(0.0, strike - underlying_price) * exercise_ratio,
         underlying_price - strike)
    } else {
        (f64::max(0.0, underlying_price - strike) * exercise_ratio,
         strike - underlying_price)
    };
    // One formula per quote side: the quote / exercise ratio is the price per
    // underlying share, plus the distance to strike. An empty side yields NaN
    // rather than a number derived from a zero quote.
    let time_value = if ask > 0.0 {
        div(ask, exercise_ratio)? + distance_to_strike
    } else {
        f64::NAN
    };
    let bid_time_value = if bid > 0.0 {
        div(bid, exercise_ratio)? + distance_to_strike
    } else {
        f64::NAN
    };
    let time_value_am = if ask > 0.0 { ask - intrinsic } else { f64::NAN };

    Ok(Some(Row {
        code: code.to_string(),
        name,
        underlying_code,
        is_put,
        underlying_price,
        ask,
        bid,
        ask_qty,
        bid_qty,
        days_to_expiry,
        strike,
        exercise_ratio,
        volume,
        time_value: round_py(time_value, 4),
        bid_time_value_pct: pct_of_spot(bid_time_value, underlying_price)?,
        ask_time_value_pct: pct_of_spot(time_value, underlying_price)?,
        time_value_am: round_py(time_value_am, 4),
    }))
}

/// `implied_vol_vec`: certified Newton plus the standard rounding-edge refine.
fn iv_vec(price: &[f64], s: &[f64], k: &[f64], t: &[f64], r: f64, ratio: &[f64],
          is_put: &[bool]) -> Vec<f64> {
    let solve = |i: usize| {
        let v = bs::implied_vol_fast(price[i], s[i], k[i], t[i], r, ratio[i], is_put[i]);
        bs::refine_row(v, price[i], s[i], k[i], t[i], r, ratio[i], is_put[i],
                       false, false, price[i], 5e-6)
    };
    if price.len() >= crate::PAR_MIN {
        (0..price.len()).into_par_iter().map(solve).collect()
    } else {
        (0..price.len()).map(solve).collect()
    }
}

/// The whole `build_warrant_df` pipeline, returning column vectors.
pub struct Columns {
    pub rows: Vec<Row>,
    pub iv_ask: Vec<f64>,
    pub iv_bid: Vec<f64>,
    pub delta: Vec<f64>,
    pub leverage: Vec<f64>,
    pub keep: Vec<bool>,
}

pub fn solve(rows: Vec<Row>, compute_iv: bool, keep_noniv: bool, r_free: f64) -> Columns {
    let n = rows.len();
    if !compute_iv {
        return Columns {
            rows,
            iv_ask: vec![f64::NAN; n],
            iv_bid: vec![f64::NAN; n],
            delta: vec![f64::NAN; n],
            leverage: vec![f64::NAN; n],
            keep: vec![true; n],
        };
    }
    let ask: Vec<f64> = rows.iter().map(|r| r.ask).collect();
    let s: Vec<f64> = rows.iter().map(|r| r.underlying_price).collect();
    let k: Vec<f64> = rows.iter().map(|r| r.strike).collect();
    let t: Vec<f64> = rows.iter().map(|r| r.days_to_expiry as f64 / 365.0).collect();
    let ratio: Vec<f64> = rows.iter().map(|r| r.exercise_ratio).collect();
    let put: Vec<bool> = rows.iter().map(|r| r.is_put).collect();

    let mut iv_ask = iv_vec(&ask, &s, &k, &t, r_free, &ratio, &put);
    // Re-solve any row whose rounded delta OR leverage could flip between the
    // Newton and brentq roots; the leverage denominator is the ask price.
    let refine = |i: usize| bs::refine_row(iv_ask[i], ask[i], s[i], k[i], t[i], r_free,
                                           ratio[i], put[i], true, true, ask[i], 5e-6);
    iv_ask = if n >= crate::PAR_MIN {
        (0..n).into_par_iter().map(refine).collect()
    } else {
        (0..n).map(refine).collect()
    };
    let bid_price: Vec<f64> = rows.iter()
        .map(|r| if r.bid > 0.0 { r.bid } else { f64::NAN })
        .collect();
    let mut iv_bid = iv_vec(&bid_price, &s, &k, &t, r_free, &ratio, &put);

    let mut delta = vec![f64::NAN; n];
    let mut leverage = vec![f64::NAN; n];
    let mut keep = vec![true; n];
    for i in 0..n {
        let converged = !iv_ask[i].is_nan();
        // iv_bid falls back to iv_ask on converged rows where bid IV is NaN.
        if converged && iv_bid[i].is_nan() {
            iv_bid[i] = iv_ask[i];
        }
        if converged {
            let d = bs::bs_delta(s[i], k[i], t[i], r_free, iv_ask[i], ratio[i], put[i]);
            delta[i] = d;
            // ask can be 0 under allow_no_quote; delta is already NaN there
            // (NaN sigma), so this stays NaN rather than inf.
            leverage[i] = s[i] * d.abs() / ask[i];
        } else {
            iv_bid[i] = f64::NAN;
        }
        // A no-ask row has no price to solve, so it can never converge — keeping
        // it explicitly is what stops the IV filter from erasing exactly the
        // one-sided books allow_no_quote let in.
        keep[i] = keep_noniv || converged || ask[i] <= 0.0;
    }
    Columns { rows, iv_ask, iv_bid, delta, leverage, keep }
}

/// Parse every CMoney payload, in the dict's own order.
pub fn parse_all<'py>(py: Python<'py>, results: &Bound<'py, PyDict>,
                      allow_no_quote: bool) -> PyResult<Vec<Row>> {
    let mut rows = Vec::with_capacity(results.len());
    for (key, value) in results.iter() {
        let code = key.str()?.to_string();
        if let Ok(Some(row)) = parse_row(py, &code, &value, allow_no_quote) {
            rows.push(row);
        }
    }
    Ok(rows)
}

/// Build the Python-side column dict; `pd.DataFrame(cols, copy=False)` wraps it.
pub fn to_columns<'py>(py: Python<'py>, c: &Columns) -> PyResult<Bound<'py, PyDict>> {
    use numpy::IntoPyArray;

    let idx: Vec<usize> = (0..c.rows.len()).filter(|&i| c.keep[i]).collect();
    let take_f = |f: &dyn Fn(&Row) -> f64| -> Vec<f64> {
        idx.iter().map(|&i| f(&c.rows[i])).collect()
    };
    let take_i = |f: &dyn Fn(&Row) -> i64| -> Vec<i64> {
        idx.iter().map(|&i| f(&c.rows[i])).collect()
    };

    let d = PyDict::new_bound(py);
    let strs = |vals: Vec<Option<String>>| -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty_bound(py);
        for v in vals {
            match v {
                Some(s) => out.append(s)?,
                None => out.append(py.None())?,
            }
        }
        Ok(out)
    };

    d.set_item("warrant_code",
               strs(idx.iter().map(|&i| Some(c.rows[i].code.clone())).collect())?)?;
    d.set_item("warrant_name",
               strs(idx.iter().map(|&i| Some(c.rows[i].name.clone())).collect())?)?;
    d.set_item("underlying_code",
               strs(idx.iter().map(|&i| c.rows[i].underlying_code.clone()).collect())?)?;
    d.set_item("type",
               strs(idx.iter()
                    .map(|&i| Some(if c.rows[i].is_put { "Put" } else { "Call" }.to_string()))
                    .collect())?)?;
    d.set_item("underlying_price", take_f(&|r| r.underlying_price).into_pyarray_bound(py))?;
    d.set_item("ask", take_f(&|r| r.ask).into_pyarray_bound(py))?;
    d.set_item("bid", take_f(&|r| r.bid).into_pyarray_bound(py))?;
    d.set_item("ask_qty", take_i(&|r| r.ask_qty).into_pyarray_bound(py))?;
    d.set_item("bid_qty", take_i(&|r| r.bid_qty).into_pyarray_bound(py))?;
    d.set_item("days_to_expiry", take_i(&|r| r.days_to_expiry).into_pyarray_bound(py))?;
    d.set_item("strike", take_f(&|r| r.strike).into_pyarray_bound(py))?;
    d.set_item("exercise_ratio", take_f(&|r| r.exercise_ratio).into_pyarray_bound(py))?;
    d.set_item("volume", take_i(&|r| r.volume).into_pyarray_bound(py))?;
    d.set_item("time_value", take_f(&|r| r.time_value).into_pyarray_bound(py))?;
    d.set_item("bid_time_value_pct", take_f(&|r| r.bid_time_value_pct).into_pyarray_bound(py))?;
    d.set_item("ask_time_value_pct", take_f(&|r| r.ask_time_value_pct).into_pyarray_bound(py))?;
    d.set_item("time_value_am", take_f(&|r| r.time_value_am).into_pyarray_bound(py))?;

    let pick = |v: &Vec<f64>| -> Vec<f64> {
        idx.iter().map(|&i| round_py(v[i], 4)).collect()
    };
    d.set_item("iv_ask", pick(&c.iv_ask).into_pyarray_bound(py))?;
    d.set_item("iv_bid", pick(&c.iv_bid).into_pyarray_bound(py))?;
    d.set_item("delta_calc", pick(&c.delta).into_pyarray_bound(py))?;
    d.set_item("leverage_calc", pick(&c.leverage).into_pyarray_bound(py))?;
    Ok(d)
}
