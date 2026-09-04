//! Bounded-variable primal simplex, specialised for the static-arb LP's shape:
//! every row is `A z >= 0`, every structural variable is boxed in `[0, u]`, and
//! `z = 0` is therefore always a feasible start (no phase 1). Columns can be
//! appended between solves without losing the basis, which is what lets
//! `static_arb.rs` grow a tiny restricted problem instead of building the whole
//! chain's dense LP.
//!
//! Kept here rather than pulled from a solver crate because the row/column
//! generation loop needs the dual vector after every solve — the price signal
//! that says which of the thousand un-enumerated legs is worth adding next —
//! and no general-purpose LP crate on crates.io hands that back.
//!
//! One warning from building it: an option chain is a nearly rank-deficient
//! matrix. Two warrants on the same strike whose expiries differ by a week have
//! columns that agree to five figures, so a basis holding both is numerically
//! singular in all but name, and product-form updates alone let `B^-1` grow
//! without bound — measured at 3e15 before the ratio test started snapping
//! variables clean across their bounds. Hence `refactorize`: `B^-1` and the
//! basic values are rebuilt from scratch on a fixed pivot cadence and whenever
//! an update looks unsound, which is what keeps the answer exact rather than
//! merely fast.

/// Reduced cost below which a column is not worth pivoting in (prices are
/// NT$/share, O(1e0..1e3), so this is ~1e-10 relative).
const DUAL_TOL: f64 = 1e-7;
/// Primal slack on variable values (shares, O(1e0..1e5)) in the ratio test.
const PRIMAL_TOL: f64 = 1e-7;
/// Pivot elements smaller than this — absolutely, or relative to the entering
/// column's largest — are treated as structurally zero.
const PIVOT_TOL: f64 = 1e-9;
const PIVOT_REL_TOL: f64 = 1e-11;
/// `B^-1 A_q` above this means the basis has gone numerically singular; the
/// iteration is redone on a fresh factorisation instead of being trusted.
const ALPHA_SANITY: f64 = 1e9;
/// Pivots between refactorisations.
const REFACTOR_EVERY: usize = 50;
/// Times a solve may abandon its basis and restart under Bland's rule before
/// the chain is declared unsolvable.
const MAX_RESTARTS: usize = 2;
/// Consecutive zero-length steps before switching to Bland's rule, which is
/// slower but cannot cycle.
const DEGENERATE_LIMIT: usize = 40;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum VarState {
    AtLower,
    AtUpper,
    Basic,
}

/// A variable: either a structural column or one row's slack. Slacks sort after
/// every structural column, which is the index order Bland's rule needs.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Var {
    Structural(usize),
    Slack(usize),
}

#[derive(Debug)]
pub enum LpFail {
    /// Ran past the iteration cap — treated as "no answer", never as "no arb".
    IterationLimit,
    /// A pivot column with no blocking row. Impossible with finite bounds on
    /// every structural column, so it means the basis went numerically bad.
    Unbounded,
    /// The basis stayed singular across repeated repairs.
    Singular,
}

/// One LP over `m` rows (`A z >= 0`) and `n` boxed columns. The row count is
/// fixed at construction; columns are stored column-major so appending one is
/// a push.
pub struct Simplex {
    m: usize,
    /// `cols[k]` holds column k's coefficient in every row, in row order.
    cols: Vec<Vec<f64>>,
    obj: Vec<f64>,
    upper: Vec<f64>,
    /// Row-major `m x m` inverse of the basis matrix.
    binv: Vec<f64>,
    basis: Vec<Var>,
    xb: Vec<f64>,
    col_state: Vec<VarState>,
    slack_state: Vec<VarState>,
    /// Scratch buffers, kept across iterations so a solve allocates nothing.
    y: Vec<f64>,
    alpha: Vec<f64>,
    work: Vec<f64>,
    rhs: Vec<f64>,
}

impl Simplex {
    /// A problem with `m` rows and no columns: the all-slack basis at `z = 0`.
    pub fn new(m: usize) -> Self {
        let mut binv = vec![0.0; m * m];
        for i in 0..m {
            binv[i * m + i] = -1.0; // the basis is -I, the slack columns
        }
        Simplex {
            m,
            cols: Vec::new(),
            obj: Vec::new(),
            upper: Vec::new(),
            binv,
            basis: (0..m).map(Var::Slack).collect(),
            xb: vec![0.0; m],
            col_state: Vec::new(),
            slack_state: vec![VarState::Basic; m],
            y: vec![0.0; m],
            alpha: vec![0.0; m],
            work: vec![0.0; m],
            rhs: vec![0.0; m],
        }
    }

    /// Append a column at its lower bound — the incumbent solution is unchanged,
    /// so the basis and `xb` stay valid.
    pub fn add_column(&mut self, coeffs: Vec<f64>, obj: f64, upper: f64) {
        debug_assert_eq!(coeffs.len(), self.m);
        self.cols.push(coeffs);
        self.obj.push(obj);
        self.upper.push(upper);
        self.col_state.push(VarState::AtLower);
    }

    /// All structural values, in column order.
    pub fn primal(&self, out: &mut Vec<f64>) {
        out.clear();
        out.resize(self.cols.len(), 0.0);
        for (k, st) in self.col_state.iter().enumerate() {
            if *st == VarState::AtUpper {
                out[k] = self.upper[k];
            }
        }
        for (pos, b) in self.basis.iter().enumerate() {
            if let Var::Structural(k) = b {
                out[*k] = self.xb[pos];
            }
        }
    }

    pub fn objective(&self, x: &[f64]) -> f64 {
        x.iter().zip(&self.obj).map(|(v, c)| v * c).sum()
    }

    /// Row prices `pi_p = -y_p >= 0` — the state-price vector the caller uses to
    /// price legs it has not enumerated into the LP yet.
    pub fn row_prices(&mut self, out: &mut Vec<f64>) {
        self.compute_duals();
        out.clear();
        out.extend(self.y.iter().map(|v| -v));
    }

    /// Rebuild `B^-1` from the basis by Gauss-Jordan with partial pivoting, then
    /// recompute the basic values from the nonbasic ones. A basis position with
    /// no usable pivot is handed back to a slack, which is always a valid basis
    /// and (with every lower bound at zero) always a feasible one.
    fn refactorize(&mut self) -> Result<(), LpFail> {
        let m = self.m;
        let mut a = vec![0.0; m * m];
        let mut rhs_col = vec![0.0; m];
        let mut pivot_row = vec![usize::MAX; m];
        let mut row_used = vec![false; m];

        // b = 0 - N x_N: only nonbasic columns at their upper bound contribute,
        // since every lower bound is zero.
        self.rhs.iter_mut().for_each(|v| *v = 0.0);
        for (k, st) in self.col_state.iter().enumerate() {
            if *st != VarState::AtUpper {
                continue;
            }
            let u = self.upper[k];
            for (p, c) in self.cols[k].iter().enumerate() {
                self.rhs[p] -= c * u;
            }
        }
        // Each pass hands at most one basis position back to a slack, and an
        // all-slack basis is never singular, so m + 1 passes always terminate.
        for _ in 0..=m {
            a.iter_mut().for_each(|v| *v = 0.0);
            for (j, b) in self.basis.iter().enumerate() {
                match b {
                    Var::Structural(k) => {
                        for i in 0..m {
                            a[i * m + j] = self.cols[*k][i];
                        }
                    }
                    Var::Slack(p) => a[*p * m + j] = -1.0,
                }
            }
            self.binv.clear();
            self.binv.resize(m * m, 0.0);
            for i in 0..m {
                self.binv[i * m + i] = 1.0;
            }
            rhs_col.copy_from_slice(&self.rhs);
            pivot_row.iter_mut().for_each(|v| *v = usize::MAX);
            row_used.iter_mut().for_each(|v| *v = false);

            let mut singular: Option<usize> = None;
            for col in 0..m {
                let (mut best, mut best_mag) = (usize::MAX, 0.0);
                for i in 0..m {
                    if row_used[i] {
                        continue;
                    }
                    let v = a[i * m + col].abs();
                    if v > best_mag {
                        best = i;
                        best_mag = v;
                    }
                }
                if best == usize::MAX || best_mag < PIVOT_TOL {
                    singular = Some(col);
                    break;
                }
                row_used[best] = true;
                pivot_row[col] = best;
                let piv = a[best * m + col];
                for j in 0..m {
                    a[best * m + j] /= piv;
                    self.binv[best * m + j] /= piv;
                }
                rhs_col[best] /= piv;
                for i in 0..m {
                    if i == best {
                        continue;
                    }
                    let f = a[i * m + col];
                    if f == 0.0 {
                        continue;
                    }
                    for j in 0..m {
                        a[i * m + j] -= f * a[best * m + j];
                        self.binv[i * m + j] -= f * self.binv[best * m + j];
                    }
                    rhs_col[i] -= f * rhs_col[best];
                }
            }

            if let Some(col) = singular {
                // Hand this basis position back to the slack of a row no basic
                // column has pivoted on, and factorize again.
                let free_row = (0..m).find(|i| !row_used[*i]).ok_or(LpFail::Singular)?;
                match self.basis[col] {
                    Var::Structural(k) => self.col_state[k] = VarState::AtLower,
                    Var::Slack(p) => self.slack_state[p] = VarState::AtLower,
                }
                self.basis[col] = Var::Slack(free_row);
                self.slack_state[free_row] = VarState::Basic;
                continue;
            }

            // Gauss-Jordan leaves a permuted identity: the reduced system's row
            // `pivot_row[col]` is basis position `col`, so B^-1's rows have to
            // be read back in that order.
            let mut ordered = vec![0.0; m * m];
            for col in 0..m {
                let src = pivot_row[col];
                ordered[col * m..(col + 1) * m].copy_from_slice(&self.binv[src * m..(src + 1) * m]);
            }
            self.binv = ordered;

            // The basic values come out of the same elimination rather than
            // from a second pass through B^-1: multiplying by an explicit
            // inverse costs about half the working digits, which on this basis
            // left a solved structure short by 0.01 NT$ at one kink — small
            // enough to look like nothing, large enough to move a lot count.
            for col in 0..m {
                self.xb[col] = rhs_col[pivot_row[col]];
            }
            return Ok(());
        }
        Err(LpFail::Singular)
    }

    /// `y^T = c_B^T B^-1`.
    fn compute_duals(&mut self) {
        let m = self.m;
        for v in self.y.iter_mut() {
            *v = 0.0;
        }
        for (i, b) in self.basis.iter().enumerate() {
            let cb = match b {
                Var::Structural(k) => self.obj[*k],
                Var::Slack(_) => 0.0,
            };
            if cb == 0.0 {
                continue;
            }
            let row = &self.binv[i * m..(i + 1) * m];
            for j in 0..m {
                self.y[j] += cb * row[j];
            }
        }
    }

    /// Reduced cost of structural column `k` (maximisation: > 0 while the column
    /// sits at its lower bound means pivoting it in raises the objective).
    fn reduced_cost(&self, k: usize) -> f64 {
        let col = &self.cols[k];
        let mut acc = self.obj[k];
        for j in 0..self.m {
            let c = col[j];
            if c != 0.0 {
                acc -= self.y[j] * c;
            }
        }
        acc
    }

    /// Throw the basis away and start from `z = 0` with every slack basic —
    /// always feasible, whatever the row set.
    fn reset_basis(&mut self) {
        self.basis.clear();
        self.basis.extend((0..self.m).map(Var::Slack));
        self.slack_state.iter_mut().for_each(|v| *v = VarState::Basic);
        self.col_state.iter_mut().for_each(|v| *v = VarState::AtLower);
        self.xb.iter_mut().for_each(|v| *v = 0.0);
    }

    /// Maximise `c^T z` from the current (feasible) basis.
    pub fn solve(&mut self) -> Result<(), LpFail> {
        self.refactorize()?;
        let cap = 200 * (self.m + self.cols.len()) + 5000;
        let mut degenerate = 0usize;
        let mut since_refactor = 0usize;
        let mut restarts = 0usize;
        let mut forced_bland = false;
        let mut it = 0usize;
        while it < cap {
            if since_refactor >= REFACTOR_EVERY {
                self.refactorize()?;
                since_refactor = 0;
            }
            self.compute_duals();
            let bland = forced_bland || degenerate >= DEGENERATE_LIMIT;
            let Some((enter, from_lower)) = self.choose_entering(bland) else {
                if since_refactor == 0 {
                    return Ok(());
                }
                // Confirm optimality on a clean factorisation before believing it.
                self.refactorize()?;
                since_refactor = 0;
                continue;
            };
            match self.pivot(enter, from_lower, bland, since_refactor == 0) {
                Ok(step) => {
                    it += 1;
                    since_refactor += 1;
                    if step <= PRIMAL_TOL {
                        degenerate += 1;
                    } else {
                        // Bland's rule is only needed to break out of a stall,
                        // and it prices far worse than Dantzig, so drop it the
                        // moment the solve is making real progress again.
                        degenerate = 0;
                        forced_bland = false;
                    }
                }
                Err(LpFail::Singular) if since_refactor > 0 => {
                    // The update trail went bad, not the problem: rebuild, retry.
                    self.refactorize()?;
                    since_refactor = 0;
                }
                Err(LpFail::Singular) | Err(LpFail::Unbounded) if restarts < MAX_RESTARTS => {
                    // Bad on a factorisation one iteration old, so rebuilding
                    // will not help: throw the basis away and re-derive it from
                    // z = 0 under Bland's rule, which takes a different pivot
                    // path and cannot cycle. Never drop the offending column —
                    // an LP solved without it reports duals that certify an
                    // optimum that is not one.
                    restarts += 1;
                    forced_bland = true;
                    degenerate = 0;
                    self.reset_basis();
                    self.refactorize()?;
                    since_refactor = 0;
                }
                Err(e) => return Err(e),
            }
        }
        Err(LpFail::IterationLimit)
    }

    /// Dantzig pricing (most attractive reduced cost), or Bland's rule (first
    /// eligible index) once the solve has stalled on degenerate pivots.
    fn choose_entering(&self, bland: bool) -> Option<(Var, bool)> {
        let mut best: Option<(Var, bool, f64)> = None;
        for k in 0..self.cols.len() {
            let from_lower = match self.col_state[k] {
                VarState::AtLower => true,
                VarState::AtUpper => false,
                VarState::Basic => continue,
            };
            let d = self.reduced_cost(k);
            let gain = if from_lower { d } else { -d };
            if gain <= DUAL_TOL {
                continue;
            }
            if bland {
                return Some((Var::Structural(k), from_lower));
            }
            if best.map_or(true, |(_, _, g)| gain > g) {
                best = Some((Var::Structural(k), from_lower, gain));
            }
        }
        for p in 0..self.m {
            // A slack column is -e_p with a zero objective, so its reduced cost
            // is y_p; it has no upper bound, so it only ever enters from below.
            if self.slack_state[p] != VarState::AtLower {
                continue;
            }
            let gain = self.y[p];
            if gain <= DUAL_TOL {
                continue;
            }
            if bland {
                return Some((Var::Slack(p), true));
            }
            if best.map_or(true, |(_, _, g)| gain > g) {
                best = Some((Var::Slack(p), true, gain));
            }
        }
        best.map(|(v, l, _)| (v, l))
    }

    fn upper_of(&self, v: Var) -> f64 {
        match v {
            Var::Structural(k) => self.upper[k],
            Var::Slack(_) => f64::INFINITY,
        }
    }

    /// Bland's variable ordering: structural columns first, then slacks.
    fn order_of(&self, v: Var) -> usize {
        match v {
            Var::Structural(k) => k,
            Var::Slack(p) => self.cols.len() + p,
        }
    }

    /// One ratio test plus basis update. Returns the step length taken.
    /// `LpFail::Singular` here means the factorisation, not the problem, is at
    /// fault and the caller should rebuild and retry. `fresh` says the
    /// factorisation is already new, so an oversized `B^-1 A_q` has nothing left
    /// to be rebuilt away and the pivot goes ahead on the largest element
    /// available: refusing it instead would leave a column that prices in
    /// permanently out of the basis, and its duals would then certify an
    /// optimum that is not one.
    fn pivot(&mut self, enter: Var, from_lower: bool, bland: bool, fresh: bool) -> Result<f64, LpFail> {
        let m = self.m;
        match enter {
            Var::Structural(k) => {
                let col = &self.cols[k];
                for i in 0..m {
                    let row = &self.binv[i * m..(i + 1) * m];
                    let mut acc = 0.0;
                    for j in 0..m {
                        let c = col[j];
                        if c != 0.0 {
                            acc += row[j] * c;
                        }
                    }
                    self.alpha[i] = acc;
                }
            }
            Var::Slack(p) => {
                for i in 0..m {
                    self.alpha[i] = -self.binv[i * m + p];
                }
            }
        }

        let amax = self.alpha.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        if amax > ALPHA_SANITY && !fresh {
            return Err(LpFail::Singular);
        }
        let piv_floor = PIVOT_TOL.max(PIVOT_REL_TOL * amax);

        let sigma = if from_lower { 1.0 } else { -1.0 };
        // Harris two-pass ratio test. Pass one takes the tightest ratio with
        // every bound loosened by PRIMAL_TOL; pass two picks the largest pivot
        // among the rows inside that window and steps by THAT row's own exact
        // ratio. Choosing the step and the leaving row together is the point:
        // a single-pass test that tie-breaks on pivot size steps by one row's
        // ratio while retiring another, which leaves the retired variable off
        // its bound by `tolerance * pivot` — unbounded error once a pivot is
        // large, and how this solver first went wrong.
        // Bland's rule only guarantees termination against the exact minimum
        // ratio, so it gets no window.
        let slack = if bland { 0.0 } else { PRIMAL_TOL };
        let mut window = self.upper_of(enter);
        for i in 0..m {
            let gamma = sigma * self.alpha[i];
            let t = if gamma > piv_floor {
                (self.xb[i] + slack) / gamma
            } else if gamma < -piv_floor {
                let ub = self.upper_of(self.basis[i]);
                if !ub.is_finite() {
                    continue;
                }
                (self.xb[i] - ub - slack) / gamma
            } else {
                continue;
            };
            if t < window {
                window = t;
            }
        }
        let window = window.max(0.0);

        let mut leave: Option<usize> = None;
        let mut leave_at_upper = false;
        let mut chosen_t = 0.0;
        let mut best_key = f64::NEG_INFINITY; // larger is preferred
        for i in 0..m {
            let gamma = sigma * self.alpha[i];
            let (t, at_upper) = if gamma > piv_floor {
                ((self.xb[i] / gamma).max(0.0), false)
            } else if gamma < -piv_floor {
                let ub = self.upper_of(self.basis[i]);
                if !ub.is_finite() {
                    continue;
                }
                (((self.xb[i] - ub) / gamma).max(0.0), true)
            } else {
                continue;
            };
            if t > window {
                continue;
            }
            // Largest pivot for numerical stability, or the lowest variable
            // index while Bland's rule is in force.
            let key = if bland {
                -(self.order_of(self.basis[i]) as f64)
            } else {
                gamma.abs()
            };
            if key > best_key {
                best_key = key;
                leave = Some(i);
                leave_at_upper = at_upper;
                chosen_t = t;
            }
        }

        let enter_span = self.upper_of(enter);
        if leave.is_none() && !enter_span.is_finite() {
            return Err(LpFail::Unbounded);
        }

        let t = if leave.is_some() { chosen_t } else { enter_span };
        let Some(r) = leave else {
            // The entering variable hit its own opposite bound: a bound flip,
            // no basis change.
            for i in 0..m {
                self.xb[i] -= sigma * t * self.alpha[i];
            }
            if let Var::Structural(k) = enter {
                self.col_state[k] = if from_lower {
                    VarState::AtUpper
                } else {
                    VarState::AtLower
                };
            }
            return Ok(t);
        };

        // The leaving variable has to land on the bound the ratio test said it
        // would. A hair past it is ordinary — the ratio clamps at zero when a
        // basic value has drifted slightly out of bounds, and the next
        // refactorisation recomputes it consistently. Landing a long way off is
        // not: that is `binv` gone bad, moving a variable clean across its
        // bound (measured once at 54,550 shares, on a pivot element of 3e15).
        let landing = self.xb[r] - sigma * t * self.alpha[r];
        let target = if leave_at_upper {
            self.upper_of(self.basis[r])
        } else {
            0.0
        };
        let scale = 1.0 + target.abs() + self.xb.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        if (landing - target).abs() > 1e-6 * scale {
            return Err(LpFail::Singular);
        }

        for i in 0..m {
            self.xb[i] -= sigma * t * self.alpha[i];
        }

        let piv = self.alpha[r];
        let enter_val = if from_lower {
            t
        } else {
            self.upper_of(enter) - t
        };

        // Product-form update of B^-1 around the pivot row.
        self.work.copy_from_slice(&self.binv[r * m..(r + 1) * m]);
        for v in self.work.iter_mut() {
            *v /= piv;
        }
        for i in 0..m {
            if i == r {
                continue;
            }
            let f = self.alpha[i];
            if f == 0.0 {
                continue;
            }
            let row = &mut self.binv[i * m..(i + 1) * m];
            for j in 0..m {
                row[j] -= f * self.work[j];
            }
        }
        self.binv[r * m..(r + 1) * m].copy_from_slice(&self.work);

        let leaving_state = if leave_at_upper {
            VarState::AtUpper
        } else {
            VarState::AtLower
        };
        match self.basis[r] {
            Var::Structural(k) => self.col_state[k] = leaving_state,
            Var::Slack(p) => self.slack_state[p] = leaving_state,
        }
        match enter {
            Var::Structural(k) => self.col_state[k] = VarState::Basic,
            Var::Slack(p) => self.slack_state[p] = VarState::Basic,
        }
        self.basis[r] = enter;
        self.xb[r] = enter_val;
        Ok(t)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call_col(kinks: &[f64], k: f64, short: bool) -> Vec<f64> {
        let s = if short { -1.0 } else { 1.0 };
        let mut c: Vec<f64> = kinks.iter().map(|x| s * (x - k).max(0.0)).collect();
        c.push(s); // tail slope
        c
    }

    /// Long call K=100 at 1.0 vs short call K=110 at 5.0, both capped at 10:
    /// the optimum is both at full size for a credit of 40.
    #[test]
    fn solves_a_two_leg_vertical() {
        let kinks = [0.0, 100.0, 110.0];
        let mut lp = Simplex::new(kinks.len() + 1);
        lp.add_column(call_col(&kinks, 100.0, false), -1.0, 10.0);
        lp.add_column(call_col(&kinks, 110.0, true), 5.0, 10.0);
        lp.solve().expect("solve");
        let mut x = Vec::new();
        lp.primal(&mut x);
        assert!((x[0] - 10.0).abs() < 1e-9, "x={x:?}");
        assert!((x[1] - 10.0).abs() < 1e-9, "x={x:?}");
        assert!((lp.objective(&x) - 40.0).abs() < 1e-9);
    }

    /// Nothing is worth doing when the long leg costs more than the short pays.
    #[test]
    fn rejects_an_unprofitable_pair() {
        let kinks = [0.0, 100.0, 110.0];
        let mut lp = Simplex::new(kinks.len() + 1);
        lp.add_column(call_col(&kinks, 100.0, false), -5.0, 10.0);
        lp.add_column(call_col(&kinks, 110.0, true), 1.0, 10.0);
        lp.solve().expect("solve");
        let mut x = Vec::new();
        lp.primal(&mut x);
        assert!(lp.objective(&x) <= 1e-9, "x={x:?}");
    }

    /// The far-right slope row alone will happily certify a long call struck
    /// ABOVE a short one as a hedge; the row at the long's own kink is what
    /// shows it is not. This is why the generation loop adds a leg's kink as a
    /// row in the same round it adds the leg.
    #[test]
    fn a_legs_own_kink_is_what_constrains_it() {
        // Rows {S=0, tail} only: short call K=100 at 3, long call K=125 at 1.
        // The tail row lets them offset one for one, for a credit of 20.
        let mut naive = Simplex::new(2);
        naive.add_column(vec![0.0, -1.0], 3.0, 10.0);
        naive.add_column(vec![0.0, 1.0], -1.0, 10.0);
        naive.solve().expect("solve");
        let mut x = Vec::new();
        naive.primal(&mut x);
        assert!((naive.objective(&x) - 20.0).abs() < 1e-9, "x={x:?}");

        // Add S=125, where the short pays 25 and the long still pays nothing.
        let mut full = Simplex::new(3);
        full.add_column(vec![0.0, -1.0, -25.0], 3.0, 10.0);
        full.add_column(vec![0.0, 1.0, 0.0], -1.0, 10.0);
        full.solve().expect("solve");
        full.primal(&mut x);
        assert!(full.objective(&x) <= 1e-9, "no credit is available: x={x:?}");
    }

    /// Two columns agreeing to nine figures — the chain's near-rank-deficiency
    /// in miniature, and what `refactorize` is there to survive.
    #[test]
    fn survives_a_near_singular_basis() {
        let kinks = [0.0, 100.0, 100.000000001, 110.0];
        let mut lp = Simplex::new(kinks.len() + 1);
        lp.add_column(call_col(&kinks, 100.0, false), -1.0, 10.0);
        lp.add_column(call_col(&kinks, 100.000000001, false), -1.0, 10.0);
        lp.add_column(call_col(&kinks, 110.0, true), 5.0, 20.0);
        lp.solve().expect("solve");
        let mut x = Vec::new();
        lp.primal(&mut x);
        // Both longs at their cap of 10 support 20 short: 5*20 - 10 - 10 = 80.
        assert!(
            (lp.objective(&x) - 80.0).abs() < 1e-6,
            "x={x:?} obj={}",
            lp.objective(&x)
        );
    }
}
