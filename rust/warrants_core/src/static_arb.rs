//! Static-arbitrage LP for one (underlying, horizon) — the Live Arb LP subtab's
//! and `/match_static_arb`'s kernel. Mirrors `logic/static_arb.py`'s
//! `_build_legs`/`_solve_lp`/`_solve_horizon` (leg normalisation, kink-constraint
//! construction, box-bounded LP, and the lot-rounding repair loop).
//!
//! The LP is never built at full size. A chain carries ~1,000 buyable legs whose
//! discounted strikes give ~700 distinct kinks, so the dense form
//! `logic/static_arb.py` hands scipy is a ~700x1,000 matrix — while the optimum
//! is supported on a few dozen legs. This module instead runs row-and-column
//! generation over `lp::Simplex`: seed with the short side, solve, read the row
//! prices back out, and pull in only the legs those prices say are mispriced,
//! adding each new leg's kink as a row at the same time. The restricted problem
//! stays ~40x150 and converges in a handful of rounds.
//!
//! That is an exact reduction, not a heuristic. On the round where nothing prices
//! in, the incumbent is primal-feasible for the whole chain (every leg it holds
//! has its own kink enforced, and a piecewise-linear payoff is >= 0 everywhere
//! iff it is >= 0 at S=0, at each kink, and in the far-right slope), and the row
//! prices extended by zero are dual-feasible for the whole chain — two matching
//! certificates, so the restricted optimum is the full optimum.
//!
//! Deliberately NOT a parity-bound port: `logic/static_arb.py`'s own module
//! docstring (and docs/adr/0004) note that `linprog(method="highs")` picks *a*
//! vertex of a structurally degenerate LP's optimal face — the value is unique,
//! the exact leg combination is not. This kernel can therefore legitimately
//! choose a different (but equally valid, equally profitable) leg combination
//! than the batch `logic/static_arb.py` path on a tie. That is why this has no
//! Python fallback registered in `logic/iv_engine.py`: presenting the slow Python
//! path as "the same feature, just slower" would be misleading when the two can
//! disagree on which vertex they land on.

use rayon::prelude::*;

use crate::arb::round_py;
use crate::lp::Simplex;

const TOL: f64 = 1e-6;
const LEG_DUST_FRAC: f64 = 1e-6;
const MAX_REPAIR_ROUNDS: usize = 25;

/// Reduced cost above which an un-enumerated leg is worth pulling into the
/// restricted LP. Same scale as the simplex's own dual tolerance.
const PRICE_TOL: f64 = 1e-7;
/// A kink becomes a row when the incumbent payoff there is negative by this
/// much of the absolute per-leg turnover at that spot. Relative because both
/// sides are sums over ~1e5-share positions, where an absolute floor would be
/// either meaningless or unreachable depending on the structure's size.
///
/// Four orders above the ~1e-16-relative noise in the sum itself, and no more:
/// at 1e-9 a structure was left standing on a 0.01 NT$ payoff shortfall, worth
/// nothing on its own but enough to lift the LP optimum off scipy's and send
/// the lot-rounding repair loop down a different eviction path.
const PAYOFF_REL_TOL: f64 = 1e-13;
/// Legs added per generation round. Small enough to keep the restricted LP
/// (and its row set, which grows with it) tight; large enough that a chain
/// needing 60 legs converges in a handful of rounds rather than sixty.
const MAX_ADD_PER_ROUND: usize = 24;
/// Generation rounds before giving up. Each round strictly adds a leg, so this
/// only ever trips on a pathological chain; hitting it is reported as an error
/// rather than as "no arb".
const MAX_GEN_ROUNDS: usize = 200;

#[derive(Clone)]
pub struct Leg {
    orig_idx: usize,
    /// 0 = warrant, 1 = option. Only meaningful on the long side, which mixes
    /// both; shorts are options only.
    kind: u8,
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
    pub long_kind: Vec<u8>,
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
    if leg.is_call {
        (s - leg.eff_strike).max(0.0)
    } else {
        (leg.eff_strike - s).max(0.0)
    }
}
fn leg_slope(leg: &Leg) -> f64 {
    if leg.is_call {
        1.0
    } else {
        0.0
    }
}
fn leg_dust(leg: &Leg) -> f64 {
    leg.lot_shares * LEG_DUST_FRAC
}
fn round6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

fn kink_points(longs: &[Leg], shorts: &[Leg]) -> Vec<f64> {
    let mut pts: Vec<f64> = vec![0.0];
    for l in longs.iter().chain(shorts.iter()) {
        pts.push(round6(l.eff_strike));
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

// ── row-and-column generation over the restricted LP ─────────────────

/// The restricted problem: which legs are columns, which kinks are rows.
///
/// The two sets are generated separately, and the row set is deliberately kept
/// far smaller than the column set. Giving every column its own kink a row
/// instead — tempting, since that makes the incumbent feasible for the whole
/// chain by construction — leaves `rows ~= columns`, which forces nearly every
/// column into the basis, near-parallel ramps included. Such a basis is
/// numerically singular (`B^-1` measured past 1e8 on a real chain) and the
/// solve returns answers that are simply wrong. With a small row set the
/// simplex has a real choice of basis and stays well conditioned.
struct Restricted {
    lp: Simplex,
    /// Kink for each row, `None` for the far-right-slope row.
    row_kink: Vec<Option<f64>>,
    /// `(is_long, index)` for each simplex column, in column order.
    col_ref: Vec<(bool, usize)>,
    in_long: Vec<bool>,
    in_short: Vec<bool>,
}

fn signed_payoff(leg: &Leg, is_long: bool, s: f64) -> f64 {
    if is_long { leg_payoff(leg, s) } else { -leg_payoff(leg, s) }
}

fn signed_slope(leg: &Leg, is_long: bool) -> f64 {
    if is_long { leg_slope(leg) } else { -leg_slope(leg) }
}

fn column_for(leg: &Leg, is_long: bool, row_kink: &[Option<f64>]) -> Vec<f64> {
    row_kink
        .iter()
        .map(|k| match k {
            Some(s) => signed_payoff(leg, is_long, *s),
            None => signed_slope(leg, is_long),
        })
        .collect()
}

impl Restricted {
    /// Build the simplex for one (rows, columns) pair from scratch. Called again
    /// whenever a row is added: a newly separated row is one the incumbent
    /// VIOLATES, so its slack cannot simply be appended as a basic variable, and
    /// `z = 0` is the one start feasible for any row set.
    fn build(row_kink: Vec<Option<f64>>, col_ref: Vec<(bool, usize)>,
             longs: &[Leg], shorts: &[Leg]) -> Self {
        let mut lp = Simplex::new(row_kink.len());
        let mut in_long = vec![false; longs.len()];
        let mut in_short = vec![false; shorts.len()];
        for &(is_long, idx) in &col_ref {
            let leg = if is_long { &longs[idx] } else { &shorts[idx] };
            let obj = if is_long { -leg.price_ps } else { leg.price_ps };
            lp.add_column(column_for(leg, is_long, &row_kink), obj, leg.depth_shares);
            if is_long { in_long[idx] = true } else { in_short[idx] = true }
        }
        Restricted { lp, row_kink, col_ref, in_long, in_short }
    }

    /// Add one leg as a column at its lower bound; the basis stays valid.
    fn add_leg(&mut self, leg: &Leg, is_long: bool, idx: usize) {
        let obj = if is_long { -leg.price_ps } else { leg.price_ps };
        self.lp
            .add_column(column_for(leg, is_long, &self.row_kink), obj, leg.depth_shares);
        self.col_ref.push((is_long, idx));
        if is_long { self.in_long[idx] = true } else { self.in_short[idx] = true }
    }

    fn has_kink(&self, k: f64) -> bool {
        self.row_kink
            .iter()
            .any(|r| matches!(r, Some(s) if (s - k).abs() < 1e-9))
    }

    /// Net payoff of the incumbent at one spot, and the summed absolute per-leg
    /// contribution there — the scale a violation has to be judged against,
    /// since both sides are sums over positions of ~1e5 shares.
    ///
    /// Summed with compensation (Neumaier). The terms reach 1e8 and cancel to
    /// near zero, so a plain sum carries ~1e-5 of noise, which is the same size
    /// as the payoff shortfalls this has to detect — a structure was certified
    /// on a real 0.01 NT$ shortfall because the threshold had to be set above
    /// that noise. Compensation drops it to ~1e-8 and lets the threshold sit
    /// far below anything that could move the answer.
    fn payoff_at(&self, x: &[f64], longs: &[Leg], shorts: &[Leg], s: f64) -> (f64, f64) {
        let mut net = 0.0f64;
        let mut comp = 0.0f64;
        let mut scale = 0.0f64;
        for (col, &(is_long, idx)) in self.col_ref.iter().enumerate() {
            if x[col] == 0.0 {
                continue;
            }
            let leg = if is_long { &longs[idx] } else { &shorts[idx] };
            let term = x[col] * signed_payoff(leg, is_long, s);
            let t = net + term;
            comp += if net.abs() >= term.abs() {
                (net - t) + term
            } else {
                (term - t) + net
            };
            net = t;
            scale += term.abs();
        }
        (net + comp, scale)
    }
}

/// `sum_p pi_p * payoff(k)` for every candidate leg, in O(log m) each: the kink
/// prices are pre-summed from both ends, so a call reads one suffix pair and a
/// put one prefix pair.
struct PriceCurve {
    kinks: Vec<f64>,
    pre_w: Vec<f64>,
    pre_ws: Vec<f64>,
    suf_w: Vec<f64>,
    suf_ws: Vec<f64>,
    tail: f64,
}

impl PriceCurve {
    fn build(row_kink: &[Option<f64>], pi: &[f64]) -> Self {
        let mut pts: Vec<(f64, f64)> = Vec::with_capacity(row_kink.len());
        let mut tail = 0.0;
        for (row, k) in row_kink.iter().enumerate() {
            match k {
                Some(s) => pts.push((*s, pi[row].max(0.0))),
                None => tail = pi[row].max(0.0),
            }
        }
        pts.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        let n = pts.len();
        let mut pre_w = vec![0.0; n + 1];
        let mut pre_ws = vec![0.0; n + 1];
        for i in 0..n {
            pre_w[i + 1] = pre_w[i] + pts[i].1;
            pre_ws[i + 1] = pre_ws[i] + pts[i].1 * pts[i].0;
        }
        let mut suf_w = vec![0.0; n + 1];
        let mut suf_ws = vec![0.0; n + 1];
        for i in (0..n).rev() {
            suf_w[i] = suf_w[i + 1] + pts[i].1;
            suf_ws[i] = suf_ws[i + 1] + pts[i].1 * pts[i].0;
        }
        PriceCurve {
            kinks: pts.into_iter().map(|(s, _)| s).collect(),
            pre_w,
            pre_ws,
            suf_w,
            suf_ws,
            tail,
        }
    }

    /// Value of one leg's payoff under the row prices.
    fn value(&self, leg: &Leg) -> f64 {
        let k = leg.eff_strike;
        if leg.is_call {
            // kinks strictly above k, each paying pi * (S - k), plus the slope row
            let j = self.kinks.partition_point(|&s| s <= k);
            self.suf_ws[j] - k * self.suf_w[j] + self.tail
        } else {
            // kinks strictly below k, each paying pi * (k - S)
            let i = self.kinks.partition_point(|&s| s < k);
            k * self.pre_w[i] - self.pre_ws[i]
        }
    }
}

/// Continuous relaxation of one horizon, solved by row-and-column generation.
/// Writes the full-length weight vectors (zero for every leg the generation loop
/// never needed) and returns the optimal entry credit.
fn solve_lp(longs: &[Leg], shorts: &[Leg], zl: &mut Vec<f64>, zs: &mut Vec<f64>) -> Result<f64, String> {
    // Rows start at {S = 0, far-right slope} plus every short leg strike: the
    // short side is what drags the payoff curve down, so those are the kinks a
    // structure is most likely to be pinned at. Columns start as the whole short
    // side — small (one expiry of bid quotes), and the entire objective.
    let mut row_kink: Vec<Option<f64>> = vec![Some(0.0), None];
    let mut col_ref: Vec<(bool, usize)> = Vec::with_capacity(shorts.len());
    for (j, leg) in shorts.iter().enumerate() {
        col_ref.push((false, j));
        let k = round6(leg.eff_strike);
        if !row_kink.iter().any(|r| matches!(r, Some(s) if (s - k).abs() < 1e-9)) {
            row_kink.push(Some(k));
        }
    }
    let mut r = Restricted::build(row_kink, col_ref, longs, shorts);

    let mut x: Vec<f64> = Vec::new();
    let mut pi: Vec<f64> = Vec::new();
    let mut converged = false;
    for _ in 0..MAX_GEN_ROUNDS {
        r.lp.solve().map_err(|e| format!("static-arb LP: {e:?}"))?;
        r.lp.primal(&mut x);

        // Row separation. The incumbent payoff bends only at the kinks of the
        // legs it actually holds, so those (plus S = 0 and the slope, always
        // rows) are the only places it can dip below zero.
        let mut new_rows: Vec<f64> = Vec::new();
        for (col, &(is_long, idx)) in r.col_ref.iter().enumerate() {
            if x[col] <= 0.0 {
                continue;
            }
            let leg = if is_long { &longs[idx] } else { &shorts[idx] };
            let k = round6(leg.eff_strike);
            if r.has_kink(k) || new_rows.iter().any(|s| (s - k).abs() < 1e-9) {
                continue;
            }
            let (net, scale) = r.payoff_at(&x, longs, shorts, k);
            if net < -PAYOFF_REL_TOL * scale {
                new_rows.push(k);
            }
        }
        if !new_rows.is_empty() {
            let mut rows = std::mem::take(&mut r.row_kink);
            rows.extend(new_rows.into_iter().map(Some));
            let cols = std::mem::take(&mut r.col_ref);
            r = Restricted::build(rows, cols, longs, shorts);
            continue;
        }

        // Column separation: price every leg not yet in, keep the best few.
        r.lp.row_prices(&mut pi);
        let curve = PriceCurve::build(&r.row_kink, &pi);
        let mut best: Vec<(f64, bool, usize)> = Vec::new();
        let consider = |rc: f64, is_long: bool, idx: usize, best: &mut Vec<(f64, bool, usize)>| {
            if rc <= PRICE_TOL {
                return;
            }
            if best.len() < MAX_ADD_PER_ROUND {
                best.push((rc, is_long, idx));
                if best.len() == MAX_ADD_PER_ROUND {
                    best.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
                }
            } else if rc > best[0].0 {
                best[0] = (rc, is_long, idx);
                best.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            }
        };
        for (i, leg) in longs.iter().enumerate() {
            if r.in_long[i] {
                continue;
            }
            consider(curve.value(leg) - leg.price_ps, true, i, &mut best);
        }
        for (j, leg) in shorts.iter().enumerate() {
            if r.in_short[j] {
                continue;
            }
            consider(leg.price_ps - curve.value(leg), false, j, &mut best);
        }
        if best.is_empty() {
            converged = true;
            break;
        }
        best.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        for &(_, is_long, idx) in &best {
            let leg = if is_long { &longs[idx] } else { &shorts[idx] };
            r.add_leg(leg, is_long, idx);
        }
    }
    if !converged {
        // Every round adds at least one row or column, so this cannot be reached
        // by a chain of any realistic size. Reported rather than swallowed: a
        // truncated generation loop has no optimality certificate, and answering
        // "no arb" off one would be a silent wrong answer.
        return Err("static-arb LP: row/column generation did not converge".into());
    }

    // The certificate is only as good as the arithmetic behind it, and this
    // chain is ill-conditioned enough to have broken a solver once already, so
    // re-derive the payoff floor from the returned weights rather than trusting
    // the simplex's own bookkeeping. Reported as an error, never as "no arb".
    for row in &r.row_kink {
        let Some(s) = row else { continue };
        let (net, scale) = r.payoff_at(&x, longs, shorts, *s);
        if net < -1e-6 * scale.max(1.0) {
            return Err(format!("static-arb LP: solution pays {net:.2} at spot {s:.2}"));
        }
    }

    zl.clear();
    zl.resize(longs.len(), 0.0);
    zs.clear();
    zs.resize(shorts.len(), 0.0);
    for (col, &(is_long, idx)) in r.col_ref.iter().enumerate() {
        if is_long {
            zl[idx] = x[col];
        } else {
            zs[idx] = x[col];
        }
    }
    Ok(r.lp.objective(&x))
}

// ── lot rounding and the repair loop ─────────────────────────────────────────

struct Rounded {
    leg: Leg,
    lots: i64,
}

fn round_longs(cand: &[Leg], z: &[f64]) -> (Vec<Rounded>, Vec<f64>, Vec<usize>) {
    let mut keep = Vec::new();
    let mut xl = Vec::new();
    let mut forced_out = Vec::new();
    for (i, (leg, &v)) in cand.iter().zip(z).enumerate() {
        if v <= leg_dust(leg) {
            continue;
        }
        let lots = ((v / leg.lot_shares - TOL).ceil()) as i64;
        if lots <= 0 {
            continue;
        }
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
        if v <= leg_dust(leg) {
            continue;
        }
        let lots = ((v / leg.lot_shares + TOL).floor()) as i64;
        if lots <= 0 {
            continue;
        }
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
        if v <= leg_dust(leg) {
            continue;
        }
        let lots = ((v / leg.lot_shares - TOL).ceil()).max(0.0);
        let cost = (lots * leg.lot_shares - v).abs() * leg.price_ps;
        if best.map_or(true, |(_, _, c)| cost > c) {
            best = Some((i, true, cost));
        }
    }
    for (j, (leg, &v)) in cand_s.iter().zip(zs).enumerate() {
        if v <= leg_dust(leg) {
            continue;
        }
        let lots = ((v / leg.lot_shares + TOL).floor()).max(0.0);
        let cost = (lots * leg.lot_shares - v).abs() * leg.price_ps;
        if best.map_or(true, |(_, _, c)| cost > c) {
            best = Some((j, false, cost));
        }
    }
    best.map(|(idx, is_long, _)| (idx, is_long))
}

/// Solve one horizon end to end (continuous relaxation + lot-rounding repair
/// loop). Mirrors static_arb.py's `_solve_horizon`. `None` means no structure —
/// either genuinely arb-free, or every repair round got evicted down to nothing
/// viable.
fn solve_horizon_legs(mut cand_l: Vec<Leg>, mut cand_s: Vec<Leg>, min_edge: f64) -> Result<Option<Solved>, String> {
    let (mut zl, mut zs) = (Vec::new(), Vec::new());
    for _ in 0..MAX_REPAIR_ROUNDS {
        if cand_l.is_empty() || cand_s.is_empty() {
            return Ok(None);
        }

        let credit = solve_lp(&cand_l, &cand_s, &mut zl, &mut zs)?;
        if credit <= min_edge.max(TOL) {
            return Ok(None);
        }

        let (keep_l, xl, forced_out) = round_longs(&cand_l, &zl);
        if !forced_out.is_empty() {
            let forced: std::collections::HashSet<usize> = forced_out.into_iter().collect();
            cand_l = cand_l
                .into_iter()
                .enumerate()
                .filter(|(i, _)| !forced.contains(i))
                .map(|(_, l)| l)
                .collect();
            continue;
        }
        let (keep_s, xs) = round_shorts(&cand_s, &zs);
        if keep_l.is_empty() || keep_s.is_empty() {
            return Ok(None);
        }

        let keep_l_legs: Vec<Leg> = keep_l.iter().map(|r| r.leg.clone()).collect();
        let keep_s_legs: Vec<Leg> = keep_s.iter().map(|r| r.leg.clone()).collect();
        let kinks2 = kink_points(&keep_l_legs, &keep_s_legs);
        let tail: f64 = keep_l.iter().zip(&xl).map(|(r, &w)| w * leg_slope(&r.leg)).sum::<f64>()
            - keep_s.iter().zip(&xs).map(|(r, &w)| w * leg_slope(&r.leg)).sum::<f64>();
        if tail < -TOL {
            return Ok(None);
        }

        let payoffs: Vec<f64> = kinks2
            .iter()
            .map(|&s| net_payoff(&keep_l_legs, &xl, &keep_s_legs, &xs, s))
            .collect();
        let min_payoff = payoffs.iter().cloned().fold(f64::INFINITY, f64::min);
        if min_payoff < -TOL {
            return Ok(None);
        }

        let gross_debit: f64 = keep_l.iter().zip(&xl).map(|(r, &w)| r.leg.price_ps * w).sum();
        let proceeds: f64 = keep_s.iter().zip(&xs).map(|(r, &w)| r.leg.price_ps * w).sum();
        let credit2 = proceeds - gross_debit;
        let guaranteed = credit2 + min_payoff.max(0.0);

        if credit2 > 0.0 && guaranteed > min_edge {
            let worst_idx = payoffs
                .iter()
                .enumerate()
                .min_by(|a, b| a.1.partial_cmp(b.1).unwrap())
                .unwrap()
                .0;
            return Ok(Some(Solved {
                long_idx: keep_l.iter().map(|r| r.leg.orig_idx).collect(),
                long_kind: keep_l.iter().map(|r| r.leg.kind).collect(),
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
            Some((idx, true)) => {
                cand_l.remove(idx);
            }
            Some((idx, false)) => {
                cand_s.remove(idx);
            }
        }
    }
    Ok(None)
}

fn legs_from_columns(price_ps: &[f64], eff_strike: &[f64], is_call: &[bool],
                     lot_shares: &[f64], depth_shares: &[f64], kind: u8) -> Vec<Leg> {
    (0..price_ps.len())
        .map(|i| Leg {
            orig_idx: i,
            kind,
            price_ps: price_ps[i],
            eff_strike: eff_strike[i],
            is_call: is_call[i],
            lot_shares: lot_shares[i],
            depth_shares: depth_shares[i],
        })
        .collect()
}

/// The continuous relaxation on its own, before any lot rounding — the single
/// number both implementations must agree on exactly, and the one the parity
/// test pins (`tests/logic/test_static_arb_lp_parity.py`). The lot-rounding
/// repair loop on top is deterministic given these weights.
#[allow(clippy::too_many_arguments)]
pub fn relaxation(
    long_price_ps: &[f64], long_eff_strike: &[f64], long_is_call: &[bool],
    long_lot_shares: &[f64], long_depth_shares: &[f64],
    short_price_ps: &[f64], short_eff_strike: &[f64], short_is_call: &[bool],
    short_lot_shares: &[f64], short_depth_shares: &[f64],
) -> Result<(f64, Vec<f64>, Vec<f64>), String> {
    let longs = legs_from_columns(long_price_ps, long_eff_strike, long_is_call,
                                  long_lot_shares, long_depth_shares, 0);
    let shorts = legs_from_columns(short_price_ps, short_eff_strike, short_is_call,
                                   short_lot_shares, short_depth_shares, 1);
    let (mut zl, mut zs) = (Vec::new(), Vec::new());
    if longs.is_empty() || shorts.is_empty() {
        return Ok((0.0, vec![0.0; longs.len()], vec![0.0; shorts.len()]));
    }
    let credit = solve_lp(&longs, &shorts, &mut zl, &mut zs)?;
    Ok((credit, zl, zs))
}

/// Column-array entry point kept for callers that build their own legs in
/// Python (`logic/live_arb_lp_logic.py`).
#[allow(clippy::too_many_arguments)]
pub fn solve_horizon(
    long_price_ps: &[f64],
    long_eff_strike: &[f64],
    long_is_call: &[bool],
    long_lot_shares: &[f64],
    long_depth_shares: &[f64],
    short_price_ps: &[f64],
    short_eff_strike: &[f64],
    short_is_call: &[bool],
    short_lot_shares: &[f64],
    short_depth_shares: &[f64],
    min_edge: f64,
) -> Result<Option<Solved>, String> {
    let cand_l = legs_from_columns(long_price_ps, long_eff_strike, long_is_call,
                                   long_lot_shares, long_depth_shares, 0);
    let cand_s = legs_from_columns(short_price_ps, short_eff_strike, short_is_call,
                                   short_lot_shares, short_depth_shares, 1);
    solve_horizon_legs(cand_l, cand_s, min_edge)
}

// ── whole-chain scan ─────────────────────────────────────────────────────────

/// One underlying's quoted chain, in the column form `logic/static_arb.py`
/// hands over. Warrant/option rows are pre-filtered by the caller exactly as
/// `match_static_arb` filters them (`ask_live | bid_live`, `min_volume`).
pub struct Chain<'a> {
    pub w_dte: &'a [i64],
    pub w_is_call: &'a [bool],
    pub w_strike: &'a [f64],
    pub w_ratio: &'a [f64],
    pub w_ask: &'a [f64],
    pub w_ask_qty: &'a [i64],
    pub o_dte: &'a [i64],
    pub o_is_call: &'a [bool],
    pub o_strike: &'a [f64],
    pub o_bid: &'a [f64],
    pub o_bid_size: &'a [i64],
    pub o_bid_live: &'a [bool],
    pub o_ask: &'a [f64],
    pub o_ask_size: &'a [i64],
    pub o_ask_live: &'a [bool],
    /// Option contract size in shares (2,000 for every TW single-stock option).
    pub m: f64,
    /// Continuous risk-free rate used to discount a longer-dated long leg's
    /// strike back to the horizon.
    pub r: f64,
}

/// One horizon's result: the structure (if any) plus the leg count dropped for
/// having a price but no resting size, which the caller reports separately.
pub struct HorizonOutcome {
    pub horizon_dte: i64,
    pub dropped_no_depth: usize,
    pub solved: Option<Solved>,
}

/// Normalise both chains to per-underlying-share legs at `t_star`. Mirrors
/// `logic/static_arb.py::_build_legs`, including its `round(price, 6)`.
fn build_legs(chain: &Chain, t_star: i64) -> (Vec<Leg>, Vec<Leg>, usize) {
    let mut longs = Vec::new();
    let mut shorts = Vec::new();
    let mut dropped = 0usize;

    for i in 0..chain.w_dte.len() {
        let dte = chain.w_dte[i];
        if dte < t_star {
            continue;
        }
        let ratio = chain.w_ratio[i];
        if !(ratio > 0.0) {
            continue;
        }
        let ask = chain.w_ask[i];
        if !(ask > 0.0) {
            continue;
        }
        let qty = chain.w_ask_qty[i];
        if qty <= 0 {
            dropped += 1;
            continue;
        }
        // A long warrant PUT keeps the intrinsic floor: it is American, so its
        // holder can exercise at the horizon. See the Python module docstring.
        let tau = ((dte - t_star) as f64 / 365.0).max(0.0);
        let is_call = chain.w_is_call[i];
        let disc = if is_call { (-chain.r * tau).exp() } else { 1.0 };
        let lot_shares = 1000.0 * ratio;
        longs.push(Leg {
            orig_idx: i,
            kind: 0,
            price_ps: round_py(ask / ratio, 6),
            eff_strike: chain.w_strike[i] * disc,
            is_call,
            lot_shares,
            depth_shares: qty as f64 * lot_shares,
        });
    }

    for j in 0..chain.o_dte.len() {
        let dte = chain.o_dte[j];
        let strike = chain.o_strike[j];
        if dte == t_star && chain.o_bid_live[j] {
            let bid = chain.o_bid[j];
            if bid > 0.0 {
                let size = chain.o_bid_size[j];
                if size <= 0 {
                    dropped += 1;
                } else {
                    shorts.push(Leg {
                        orig_idx: j,
                        kind: 1,
                        price_ps: round_py(bid, 6),
                        eff_strike: strike,
                        is_call: chain.o_is_call[j],
                        lot_shares: chain.m,
                        depth_shares: size as f64 * chain.m,
                    });
                }
            }
        }
        if dte >= t_star && chain.o_ask_live[j] {
            let ask = chain.o_ask[j];
            if ask > 0.0 {
                let size = chain.o_ask_size[j];
                if size <= 0 {
                    dropped += 1;
                } else {
                    let tau = ((dte - t_star) as f64 / 365.0).max(0.0);
                    longs.push(Leg {
                        orig_idx: j,
                        kind: 1,
                        price_ps: round_py(ask, 6),
                        eff_strike: strike * (-chain.r * tau).exp(),
                        is_call: chain.o_is_call[j],
                        lot_shares: chain.m,
                        depth_shares: size as f64 * chain.m,
                    });
                }
            }
        }
    }

    (longs, shorts, dropped)
}

/// Build and solve every horizon of one chain, in parallel. `horizons` is the
/// caller's already-filtered expiry list (the dates a short leg can settle on).
pub fn scan(chain: &Chain, horizons: &[i64], min_edge: f64) -> Result<Vec<HorizonOutcome>, String> {
    horizons
        .par_iter()
        .map(|&t_star| {
            let (longs, shorts, dropped) = build_legs(chain, t_star);
            let solved = if longs.is_empty() || shorts.is_empty() {
                None
            } else {
                solve_horizon_legs(longs, shorts, min_edge)?
            };
            Ok(HorizonOutcome { horizon_dte: t_star, dropped_no_depth: dropped, solved })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_a_riskless_vertical() {
        // Long call (strike 100, price 1, upper 10) vs short call (strike
        // 110, price 5, upper 10): the optimum is x=y=10, credit=40.
        let res = solve_horizon(
            &[1.0], &[100.0], &[true], &[1.0], &[10.0],
            &[5.0], &[110.0], &[true], &[1.0], &[10.0],
            0.0,
        )
        .expect("solve")
        .expect("structure");
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
        )
        .expect("solve");
        assert!(res.is_none());
    }

    /// A long call struck ABOVE the short cannot hedge it — the generation loop
    /// must add that kink as a row before it certifies anything.
    #[test]
    fn rejects_an_unhedged_short() {
        let res = solve_horizon(
            &[1.0], &[130.0], &[true], &[1.0], &[10.0],
            &[5.0], &[110.0], &[true], &[1.0], &[10.0],
            0.0,
        )
        .expect("solve");
        assert!(res.is_none(), "a higher-struck long call is not a hedge");
    }

    /// Convexity: wings at 100/120 bought against a rich 110 body.
    #[test]
    fn finds_a_butterfly() {
        let res = solve_horizon(
            &[3.0, 2.0], &[100.0, 120.0], &[true, true], &[1.0, 1.0], &[10.0, 10.0],
            &[8.0], &[110.0], &[true], &[1.0], &[20.0],
            0.0,
        )
        .expect("solve")
        .expect("structure");
        // Both wings at their 10-unit cap; the body is then capped at 20 by
        // the tail slope and by the payoff at S=120: 8*20 - 3*10 - 2*10 = 110.
        assert!((res.net_credit - 110.0).abs() < 1e-6, "net_credit={}", res.net_credit);
        assert_eq!(res.long_lots, vec![10, 10]);
        assert_eq!(res.short_lots, vec![20]);
    }
}
