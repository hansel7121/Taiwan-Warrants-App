//! Arbitrage matcher kernels.
//!
//! These mirror `logic/arb_logic.py` exactly, including its rounding: the Python
//! code rounds with the builtin `round(x, n)`, which is correctly-rounded decimal
//! rounding (ties to even) rather than NumPy's scale-rint-unscale, so `round_py`
//! below formats and re-parses to reproduce it.

/// Python's builtin `round(x, nd)`: correctly-rounded to `nd` decimal places,
/// ties to even. Rust's float formatting has the same contract, so a format and
/// re-parse round-trip agrees with CPython bit for bit.
thread_local! {
    /// Reused so the hot paths (one round per emitted column, per row) do not
    /// allocate a String each time.
    static ROUND_BUF: std::cell::RefCell<String> = std::cell::RefCell::new(String::with_capacity(48));
}

#[inline]
pub fn round_py(x: f64, nd: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    use std::fmt::Write;
    ROUND_BUF.with(|b| {
        let mut buf = b.borrow_mut();
        buf.clear();
        let _ = write!(buf, "{:.*}", nd, x);
        buf.parse::<f64>().unwrap_or(x)
    })
}

/// One surviving butterfly: the two wing positions, the chosen body, and the
/// economics that decided it.
pub struct Butterfly {
    pub wing_lo: i64,
    pub wing_hi: i64,
    pub body: i64,
    pub credit_ps: f64,
    pub tail: f64,
    pub worst_payoff_ps: f64,
    pub guaranteed_ps: f64,
}

/// Every wing pair whose best body yields a locked-in credit.
///
/// Wings arrive sorted by strike (the Python side iterates `sorted(wings)`).
/// Bodies arrive sorted by strike as well, carrying their original chain
/// position in `body_orig`, because ties on `sell_ps` keep the body that came
/// first in the chain — which is what `max()` over the filtered list did.
///
/// The `guaranteed_ps > 0` test is the one place rounding can change the answer,
/// and only when the unrounded value sits within half a rounding step of zero;
/// everything outside that band short-circuits, so the expensive decimal
/// rounding runs for survivors and near-ties only.
#[allow(clippy::too_many_arguments)]
pub fn butterfly_pairs(
    wing_k: &[f64],
    wing_buy: &[f64],
    wing_dte: &[i64],
    body_k: &[f64],
    body_sell: &[f64],
    body_dte: &[i64],
    body_orig: &[i64],
    is_call: bool,
) -> Vec<Butterfly> {
    let mut out = Vec::new();
    let nw = wing_k.len();
    let nb = body_k.len();
    if nw < 2 || nb == 0 {
        return out;
    }

    for a in 0..nw {
        let k1 = wing_k[a];
        // Strictly between the wings: skip bodies at or below the low strike.
        let lo = upper_bound(body_k, k1);
        for b in (a + 1)..nw {
            let k2 = wing_k[b];
            let hi = lower_bound(body_k, k2);
            if lo >= hi {
                continue;
            }
            let dte_cap = wing_dte[a].min(wing_dte[b]);

            let mut best: isize = -1;
            for i in lo..hi {
                if body_dte[i] > dte_cap {
                    continue;
                }
                if best < 0 {
                    best = i as isize;
                    continue;
                }
                let cur = body_sell[best as usize];
                if body_sell[i] > cur
                    || (body_sell[i] == cur && body_orig[i] < body_orig[best as usize])
                {
                    best = i as isize;
                }
            }
            if best < 0 {
                continue;
            }
            let bi = best as usize;

            let x = body_k[bi];
            let raw_credit = 2.0 * body_sell[bi] - wing_buy[a] - wing_buy[b];
            let tail = if is_call {
                2.0 * x - k1 - k2
            } else {
                k1 + k2 - 2.0 * x
            };
            let worst = f64::min(0.0, tail);
            let approx = worst + raw_credit;
            // Outside +/- 1e-6 the rounding cannot flip the sign: credit is
            // rounded to 6 dp before the sum, so both roundings together move
            // the total by at most 1e-6.
            if approx < -1e-6 {
                continue;
            }
            let credit_ps = round_py(raw_credit, 6);
            let guaranteed_ps = round_py(worst + credit_ps, 6);
            if guaranteed_ps <= 0.0 {
                continue;
            }
            out.push(Butterfly {
                wing_lo: a as i64,
                wing_hi: b as i64,
                body: body_orig[bi],
                credit_ps,
                tail,
                worst_payoff_ps: worst,
                guaranteed_ps,
            });
        }
    }
    out
}

/// First index whose value is strictly greater than `v` (sorted ascending).
fn upper_bound(xs: &[f64], v: f64) -> usize {
    let (mut lo, mut hi) = (0usize, xs.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        if xs[mid] > v {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    lo
}

/// First index whose value is greater than or equal to `v` (sorted ascending).
fn lower_bound(xs: &[f64], v: f64) -> usize {
    let (mut lo, mut hi) = (0usize, xs.len());
    while lo < hi {
        let mid = (lo + hi) / 2;
        if xs[mid] >= v {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    lo
}

/// One emitted warrant/option pair from the same-type matcher.
pub struct DirectHit {
    pub wi: i64,
    pub oi: i64,
    pub direction: i64,
    pub price_diff: f64,
    pub exec_opt: f64,
    pub exec_warrant: f64,
    pub strike_diff_pct: f64,
    pub dte_diff: i64,
    pub favorable: bool,
    pub max_loss_per_share: f64,
}

/// Every pair the same-type matcher would emit, in emission order: direction,
/// then warrant, then option. The caller applies the (warrant, contract) dedup
/// and builds the rows.
///
/// NaN quotes propagate exactly as they did per row — `NaN <= 0` is false, so a
/// pair priced off a missing side still reaches the caller.
#[allow(clippy::too_many_arguments)]
pub fn direct_pairs(
    w_type: &[i64], w_is_put: &[bool], w_strike: &[f64], w_dte: &[i64],
    w_ratio: &[f64], w_ask: &[f64], w_bid: &[f64],
    o_type: &[i64], o_strike: &[f64], o_dte: &[i64], o_bid: &[f64], o_ask: &[f64],
    o_bid_live: &[bool], o_ask_live: &[bool],
    max_dte_diff: i64, positive_loose: bool,
) -> Vec<DirectHit> {
    let mut out = Vec::new();
    let nw = w_strike.len();
    let no = o_strike.len();
    if nw == 0 || no == 0 {
        return out;
    }

    for direction in 0..2i64 {
        let positive = direction == 0;
        for wi in 0..nw {
            let kw = w_strike[wi];
            let dte_w = w_dte[wi];
            let ratio = w_ratio[wi];
            if ratio <= 0.0 {
                continue; // can't size or normalise a warrant with no exercise ratio
            }
            // The residual vertical must be FAVORABLE (payoff >= 0 everywhere)
            // so the entry credit is never clawed back at expiry.
            let opt_ge_warrant = positive != w_is_put[wi];
            let ask_ps = round_py(w_ask[wi] / ratio, 4);
            let bid_live = w_bid[wi] > 0.0;
            // Loose marks tolerate the ask-as-bid fallback; an EXECUTABLE sell
            // does not — nobody lifts a bid that isn't there.
            let bid_ps = round_py(
                (if bid_live { w_bid[wi] } else { w_ask[wi] }) / ratio, 4);
            let bid_real_ps = if bid_live {
                Some(round_py(w_bid[wi] / ratio, 4))
            } else {
                None
            };
            let typ_w = w_type[wi];

            for oi in 0..no {
                if o_type[oi] != typ_w {
                    continue;
                }
                let ko = o_strike[oi];
                let favorable = if opt_ge_warrant { ko >= kw } else { ko <= kw };
                if !favorable {
                    continue;
                }
                let dte_o = o_dte[oi];
                // Never hold the SHORT leg as the longer-dated one — the hedge
                // leg would expire first, leaving a naked short position.
                if positive {
                    if dte_o > dte_w {
                        continue;
                    }
                } else if (dte_w - dte_o).max(0) > max_dte_diff {
                    continue;
                }

                if !(o_ask_live[oi] || o_bid_live[oi]) {
                    continue;
                }
                let o_bid_ps = if o_bid_live[oi] { Some(round_py(o_bid[oi], 4)) } else { None };
                let o_ask_ps = if o_ask_live[oi] { Some(round_py(o_ask[oi], 4)) } else { None };

                // Tight = prices you can actually hit. Loose = optimistic
                // favorable-side mark, for ranking only.
                let (price_diff, exec_opt, exec_warrant) = if positive {
                    let (px, eo, ew) = if positive_loose {
                        let Some(a) = o_ask_ps else { continue };
                        (round_py(a - bid_ps, 4), a, bid_ps)
                    } else {
                        let Some(b) = o_bid_ps else { continue };
                        (round_py(b - ask_ps, 4), b, ask_ps)
                    };
                    if px <= 0.0 {
                        continue;
                    }
                    (px, eo, ew)
                } else {
                    let (px, eo, ew) = if positive_loose {
                        let Some(b) = o_bid_ps else { continue };
                        (round_py(b - ask_ps, 4), b, ask_ps)
                    } else {
                        // A synthetic (ask-as-bid) warrant bid would fake the
                        // proceeds, so demand the REAL one.
                        let (Some(a), Some(br)) = (o_ask_ps, bid_real_ps) else { continue };
                        (round_py(a - br, 4), a, br)
                    };
                    if px >= 0.0 {
                        continue;
                    }
                    (px, eo, ew)
                };

                out.push(DirectHit {
                    wi: wi as i64,
                    oi: oi as i64,
                    direction,
                    price_diff,
                    exec_opt,
                    exec_warrant,
                    strike_diff_pct: (ko - kw).abs() / kw * 100.0,
                    dte_diff: (dte_o - dte_w).abs(),
                    favorable,
                    max_loss_per_share: if favorable { 0.0 } else { (ko - kw).abs() },
                });
            }
        }
    }
    out
}
