//! Static-arbitrage LP for one (underlying, horizon) — the Live Arb LP
//! subtab's kernel. Mirrors `logic/static_arb.py`'s `_solve_lp`/
//! `_solve_horizon` (kink-constraint construction, box-bounded LP, and the
//! lot-rounding repair loop), using the pure-Rust `microlp` crate instead of
//! scipy/HiGHS.
//!
//! Deliberately NOT a parity-bound port: `logic/static_arb.py`'s own module
//! docstring (and docs/adr/0004) note that `linprog(method="highs")` picks
//! *a* vertex of a structurally degenerate LP's optimal face — the value is
//! unique, the exact leg combination is not. This kernel can therefore
//! legitimately choose a different (but equally valid, equally profitable)
//! leg combination than the batch `logic/static_arb.py` path on a tie. That
//! is why this has no Python fallback registered in `logic/iv_engine.py`:
//! presenting the slow Python path as "the same feature, just slower" would
//! be misleading when the two can disagree on which vertex they land on.
//!
//! Validated (see the plan this shipped under) against `logic/static_arb.py`
//! on a ~1000-warrant/144-option synthetic TSMC-scale basket: identical
//! `net_credit`/`guaranteed_profit`/`min_payoff`/`gross_debit`/`return_pct`
//! and identical leg selection on every horizon tested, at roughly 20x the
//! wall-clock speed once parallelized across horizons (a job for the Python
//! caller via `rayon`/threads at the orchestration layer, not this module).

use microlp::{ComparisonOp, Error as LpError, OptimizationDirection, Problem, SolveOutcome};

const TOL: f64 = 1e-6;
const LEG_DUST_FRAC: f64 = 1e-6;
const MAX_REPAIR_ROUNDS: usize = 25;

#[derive(Clone)]
struct Leg {
    orig_idx: usize,
    price_ps: f64,
    eff_strike: f64,
    is_call: bool,
    lot_shares: f64,
    depth_shares: f64,
}

/// One solved static-arb structure. Indices are positions into the ORIGINAL
/// input arrays the caller passed in — this module never hands back a leg's
/// code/name/strike/etc; the Python caller already has that data and just
/// needs to know which candidates were used and at how many lots.
pub struct Solved {
    pub long_idx: Vec<usize>,
    pub long_lots: Vec<i64>,
    pub short_idx: Vec<usize>,
    pub short_lots: Vec<i64>,
    pub net_credit: f64,
    pub min_payoff: f64,
    pub guaranteed_profit: f64,
    pub worst_spot: f64,
    pub gross_debit: f64,
}

fn leg_payoff(leg: &Leg, s: f64) -> f64 {
    if leg.is_call { (s - leg.eff_strike).max(0.0) } else { (leg.eff_strike - s).max(0.0) }
}
fn leg_slope(leg: &Leg) -> f64 {
    if leg.is_call { 1.0 } else { 0.0 }
}
fn leg_dust(leg: &Leg) -> f64 {
    leg.lot_shares * LEG_DUST_FRAC
}

fn kink_points(longs: &[Leg], shorts: &[Leg]) -> Vec<f64> {
    let mut pts: Vec<f64> = vec![0.0];
    for l in longs.iter().chain(shorts.iter()) {
        pts.push((l.eff_strike * 1e6).round() / 1e6);
    }
    pts.sort_by(|a, b| a.partial_cmp(b).unwrap());
    pts.dedup_by(|a, b| (*a - *b).abs() < 1e-9);
    pts
}

fn net_payoff(longs: &[Leg], xl: &[f64], shorts: &[Leg], xs: &[f64], s: f64) -> f64 {
    let long_part: f64 = longs.iter().zip(xl).map(|(l, &w)| w * leg_payoff(l, s)).sum();
    let short_part: f64 = shorts.iter().zip(xs).map(|(l, &w)| w * leg_payoff(l, s)).sum();
    long_part - short_part
}

/// One continuous LP relaxation solve. Mirrors static_arb.py's `_solve_lp`:
/// maximize sum(short proceeds) - sum(long cost), subject to the net payoff
/// being >= 0 at every kink and at the far-right slope, 0 <= x <= depth per
/// leg. Returns (credit, weights-per-long, weights-per-short).
fn solve_lp(longs: &[Leg], shorts: &[Leg]) -> Result<(f64, Vec<f64>, Vec<f64>), LpError> {
    let kinks = kink_points(longs, shorts);
    let mut problem = Problem::new(OptimizationDirection::Maximize);

    let long_vars: Vec<_> = longs.iter()
        .map(|l| problem.add_var(-l.price_ps, (0.0, l.depth_shares)))
        .collect();
    let short_vars: Vec<_> = shorts.iter()
        .map(|s| problem.add_var(s.price_ps, (0.0, s.depth_shares)))
        .collect();

    for &s in &kinks {
        let mut expr: Vec<(microlp::Variable, f64)> = Vec::with_capacity(longs.len() + shorts.len());
        for (i, l) in longs.iter().enumerate() {
            expr.push((long_vars[i], leg_payoff(l, s)));
        }
        for (j, sh) in shorts.iter().enumerate() {
            expr.push((short_vars[j], -leg_payoff(sh, s)));
        }
        problem.add_constraint(&expr, ComparisonOp::Ge, 0.0);
    }
    let mut tail_expr: Vec<(microlp::Variable, f64)> = Vec::with_capacity(longs.len() + shorts.len());
    for (i, l) in longs.iter().enumerate() {
        tail_expr.push((long_vars[i], leg_slope(l)));
    }
    for (j, sh) in shorts.iter().enumerate() {
        tail_expr.push((short_vars[j], -leg_slope(sh)));
    }
    problem.add_constraint(&tail_expr, ComparisonOp::Ge, 0.0);

    let outcome = problem.solve()?;
    let sol = match outcome {
        SolveOutcome::Solution(s) => s,
        SolveOutcome::Interrupted(_) => {
            return Err(LpError::InternalError("unexpected interruption (no time/node limit was set)".into()));
        }
    };
    let credit = sol.objective();
    let zl: Vec<f64> = long_vars.iter().map(|&v| sol.var_value(v)).collect();
    let zs: Vec<f64> = short_vars.iter().map(|&v| sol.var_value(v)).collect();
    Ok((credit, zl, zs))
}

struct Rounded {
    leg: Leg,
    lots: i64,
}

fn round_longs(cand: &[Leg], z: &[f64]) -> (Vec<Rounded>, Vec<f64>, Vec<usize>) {
    let mut keep = Vec::new();
    let mut xl = Vec::new();
    let mut forced_out = Vec::new();
    for (i, (leg, &v)) in cand.iter().zip(z).enumerate() {
        if v <= leg_dust(leg) { continue; }
        let lots = ((v / leg.lot_shares - TOL).ceil()) as i64;
        if lots <= 0 { continue; }
        let shares = lots as f64 * leg.lot_shares;
        if shares > leg.depth_shares + TOL {
            forced_out.push(i);
            continue;
        }
        xl.push(shares);
        keep.push(Rounded { leg: leg.clone(), lots });
    }
    (keep, xl, forced_out)
}

fn round_shorts(cand: &[Leg], z: &[f64]) -> (Vec<Rounded>, Vec<f64>) {
    let mut keep = Vec::new();
    let mut xs = Vec::new();
    for (leg, &v) in cand.iter().zip(z) {
        if v <= leg_dust(leg) { continue; }
        let lots = ((v / leg.lot_shares + TOL).floor()) as i64;
        if lots <= 0 { continue; }
        let shares = lots as f64 * leg.lot_shares;
        xs.push(shares);
        keep.push(Rounded { leg: leg.clone(), lots });
    }
    (keep, xs)
}

/// Index (into `cand_l`/`cand_s`) and side of whichever leg's lot-rounding
/// cost the most in cash terms — mirrors static_arb.py's `_worst_rounded_leg`.
fn worst_rounded_leg(cand_l: &[Leg], zl: &[f64], cand_s: &[Leg], zs: &[f64]) -> Option<(usize, bool)> {
    let mut best: Option<(usize, bool, f64)> = None;
    for (i, (leg, &v)) in cand_l.iter().zip(zl).enumerate() {
        if v <= leg_dust(leg) { continue; }
        let lots = ((v / leg.lot_shares - TOL).ceil()).max(0.0);
        let shares = lots * leg.lot_shares;
        let cost = (shares - v).abs() * leg.price_ps;
        if best.map_or(true, |(_, _, c)| cost > c) { best = Some((i, true, cost)); }
    }
    for (j, (leg, &v)) in cand_s.iter().zip(zs).enumerate() {
        if v <= leg_dust(leg) { continue; }
        let lots = ((v / leg.lot_shares + TOL).floor()).max(0.0);
        let shares = lots * leg.lot_shares;
        let cost = (shares - v).abs() * leg.price_ps;
        if best.map_or(true, |(_, _, c)| cost > c) { best = Some((j, false, cost)); }
    }
    best.map(|(idx, is_long, _)| (idx, is_long))
}

/// Solve one horizon end to end (continuous relaxation + lot-rounding repair
/// loop). Mirrors static_arb.py's `_solve_horizon`. `None` means no
/// structure — either genuinely arb-free, or every repair round got evicted
/// down to nothing viable.
pub fn solve_horizon(
    long_price_ps: &[f64], long_eff_strike: &[f64], long_is_call: &[bool],
    long_lot_shares: &[f64], long_depth_shares: &[f64],
    short_price_ps: &[f64], short_eff_strike: &[f64], short_is_call: &[bool],
    short_lot_shares: &[f64], short_depth_shares: &[f64],
    min_edge: f64,
) -> Result<Option<Solved>, String> {
    let mut cand_l: Vec<Leg> = (0..long_price_ps.len())
        .map(|i| Leg {
            orig_idx: i, price_ps: long_price_ps[i], eff_strike: long_eff_strike[i],
            is_call: long_is_call[i], lot_shares: long_lot_shares[i], depth_shares: long_depth_shares[i],
        })
        .collect();
    let mut cand_s: Vec<Leg> = (0..short_price_ps.len())
        .map(|i| Leg {
            orig_idx: i, price_ps: short_price_ps[i], eff_strike: short_eff_strike[i],
            is_call: short_is_call[i], lot_shares: short_lot_shares[i], depth_shares: short_depth_shares[i],
        })
        .collect();

    for _round in 0..MAX_REPAIR_ROUNDS {
        if cand_l.is_empty() || cand_s.is_empty() { return Ok(None); }

        let (credit, zl, zs) = match solve_lp(&cand_l, &cand_s) {
            Ok(v) => v,
            Err(LpError::Infeasible) => return Ok(None),
            Err(e) => return Err(format!("static-arb LP solve failed: {e}")),
        };
        if credit <= min_edge.max(TOL) { return Ok(None); }

        let (keep_l, xl, forced_out) = round_longs(&cand_l, &zl);
        if !forced_out.is_empty() {
            let forced: std::collections::HashSet<usize> = forced_out.into_iter().collect();
            cand_l = cand_l.into_iter().enumerate()
                .filter(|(i, _)| !forced.contains(i)).map(|(_, l)| l).collect();
            continue;
        }
        let (keep_s, xs) = round_shorts(&cand_s, &zs);
        if keep_l.is_empty() || keep_s.is_empty() { return Ok(None); }

        let keep_l_legs: Vec<Leg> = keep_l.iter().map(|r| r.leg.clone()).collect();
        let keep_s_legs: Vec<Leg> = keep_s.iter().map(|r| r.leg.clone()).collect();
        let kinks2 = kink_points(&keep_l_legs, &keep_s_legs);
        let tail: f64 = keep_l.iter().zip(&xl).map(|(r, &w)| w * leg_slope(&r.leg)).sum::<f64>()
            - keep_s.iter().zip(&xs).map(|(r, &w)| w * leg_slope(&r.leg)).sum::<f64>();
        if tail < -TOL { return Ok(None); }

        let payoffs: Vec<f64> = kinks2.iter()
            .map(|&s| net_payoff(&keep_l_legs, &xl, &keep_s_legs, &xs, s)).collect();
        let min_payoff = payoffs.iter().cloned().fold(f64::INFINITY, f64::min);
        if min_payoff < -TOL { return Ok(None); }

        let gross_debit: f64 = keep_l.iter().zip(&xl).map(|(r, &w)| r.leg.price_ps * w).sum();
        let proceeds: f64 = keep_s.iter().zip(&xs).map(|(r, &w)| r.leg.price_ps * w).sum();
        let credit2 = proceeds - gross_debit;
        let guaranteed = credit2 + min_payoff.max(0.0);

        if credit2 > 0.0 && guaranteed > min_edge {
            let worst_idx = payoffs.iter().enumerate()
                .min_by(|a, b| a.1.partial_cmp(b.1).unwrap()).unwrap().0;
            return Ok(Some(Solved {
                long_idx: keep_l.iter().map(|r| r.leg.orig_idx).collect(),
                long_lots: keep_l.iter().map(|r| r.lots).collect(),
                short_idx: keep_s.iter().map(|r| r.leg.orig_idx).collect(),
                short_lots: keep_s.iter().map(|r| r.lots).collect(),
                net_credit: credit2.round(),
                min_payoff: min_payoff.round(),
                guaranteed_profit: guaranteed.round(),
                worst_spot: (kinks2[worst_idx] * 100.0).round() / 100.0,
                gross_debit: gross_debit.round(),
            }));
        }

        match worst_rounded_leg(&cand_l, &zl, &cand_s, &zs) {
            None => return Ok(None),
            Some((idx, true)) => { cand_l.remove(idx); }
            Some((idx, false)) => { cand_s.remove(idx); }
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_a_riskless_vertical() {
        // Long call (strike 100, price 1, upper 10) vs short call (strike
        // 110, price 5, upper 10) -- hand-verified in the plan this shipped
        // under: optimum is x=y=10, credit=40.
        let res = solve_horizon(
            &[1.0], &[100.0], &[true], &[1.0], &[10.0],
            &[5.0], &[110.0], &[true], &[1.0], &[10.0],
            0.0,
        ).expect("solve").expect("structure");
        assert!((res.net_credit - 40.0).abs() < 1e-6, "net_credit={}", res.net_credit);
        assert_eq!(res.long_lots, vec![10]);
        assert_eq!(res.short_lots, vec![10]);
    }

    #[test]
    fn no_structure_when_unprofitable() {
        let res = solve_horizon(
            &[5.0], &[100.0], &[true], &[1.0], &[10.0],
            &[1.0], &[110.0], &[true], &[1.0], &[10.0],
            0.0,
        ).expect("solve");
        assert!(res.is_none());
    }
}
