//! Per-tick time-value arithmetic for the Live Warrant tab.
//!
//! Deliberately does NOT solve IV, delta or leverage — those are never
//! computed for this feature, not even as an unexposed intermediate, so this
//! file has no dependency on `bs`'s solver at all. Only the time-value block
//! that `frame.rs::parse_row` computes inline (lines ~161-180) is ported,
//! as a standalone function callable for one row without going through the
//! whole-frame `build_warrant_columns` pipeline.

use crate::arb::round_py;

/// One warrant's time-value columns for its current best bid/ask.
pub struct TickResult {
    pub time_value: f64,
    pub bid_time_value_pct: f64,
    pub ask_time_value_pct: f64,
}

/// `x / y`, or NaN when `y` is zero — a live tick must never panic/error on a
/// bad ratio, it should just degrade that one field to NaN (rendered "—").
fn div_or_nan(x: f64, y: f64) -> f64 {
    if y == 0.0 { f64::NAN } else { x / y }
}

/// Value as a % of spot, rounded the way `frame.rs::pct_of_spot` rounds it.
fn pct_of_spot(value: f64, s: f64) -> f64 {
    if !value.is_finite() {
        return f64::NAN;
    }
    round_py(div_or_nan(value, s) * 100.0, 4)
}

/// `time_value`/`bid_time_value_pct`/`ask_time_value_pct` for one warrant,
/// given its strike/ratio/side and current best bid/ask. Mirrors
/// `frame.rs::parse_row` lines 161-180 exactly, minus `intrinsic` (only
/// `time_value_am` needed it, and that column is dropped) and minus any IV
/// input (`T`/`r` are not needed here at all since no solver is invoked).
pub fn solve_tick(s: f64, k: f64, ratio: f64, is_put: bool, bid: f64, ask: f64) -> TickResult {
    let distance_to_strike = if is_put { s - k } else { k - s };

    let time_value = if ask > 0.0 {
        div_or_nan(ask, ratio) + distance_to_strike
    } else {
        f64::NAN
    };
    let bid_time_value = if bid > 0.0 {
        div_or_nan(bid, ratio) + distance_to_strike
    } else {
        f64::NAN
    };

    TickResult {
        time_value: round_py(time_value, 4),
        bid_time_value_pct: pct_of_spot(bid_time_value, s),
        ask_time_value_pct: pct_of_spot(time_value, s),
    }
}
