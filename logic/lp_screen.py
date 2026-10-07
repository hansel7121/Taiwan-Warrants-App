"""Warm-started screen for the static-arb LP, used by the end-of-day replay
(logic/eod_arb_replay.py). One persistent HiGHS model per horizon holds the
same LP relaxation logic/static_arb.py::_solve_lp builds; a tick only changes
one instrument's price/depth, so re-solving from the previous basis is
milliseconds where rebuilding from scratch is ~1s on a full TSMC chain.

static_arb._solve_horizon returns None whenever that relaxation's value is at
or below max(min_edge, _TOL), so the replay only calls the full solver when
this screen says the relaxation is above it — the screened answer is exactly
the full solver's answer, just without solving it on every tick.

Most ticks need no HiGHS run at all. The LP's constraints are homogeneous
(payoff >= 0 is a cone, depths only cap it), so once the relaxation is exactly
0 it stays 0 until some leg gets strictly better for us — a long's ask or a
short's bid improving, or a pulled leg becoming tradable again. A worse price,
a pulled quote or a resting-size change on a still-quoted leg cannot create an
arb, so those ticks skip the solve.
"""
import highspy
import numpy as np

from logic import static_arb

# Skip the full solver only when the relaxation is at most this fraction of
# its own cutoff. An arb-free book's relaxation is exactly 0 (the empty
# portfolio) in both HiGHS builds, and the two agree to ~1e-9 relative
# elsewhere, so this band never hides a relaxation the full solver would pass.
SCREEN_FRACTION = 0.5


def leg_key(leg, side):
    return (side, leg["kind"], leg["code"])


def _static(leg):
    return (round(float(leg["eff_strike"]), 6), bool(leg["is_call"]), float(leg["lot_shares"]))


class HorizonScreen:
    """The LP relaxation for one horizon, updated one instrument at a time."""

    def __init__(self):
        self.h = None
        self.cols = {}      # leg key -> column index
        self.static = {}    # leg key -> (eff_strike, is_call, lot_shares)
        self.by_code = {}   # instrument code -> set of leg keys
        self.cost = None    # current column costs / upper bounds, mirrored from the model
        self.upper = None
        self.last_relax = None   # value of the most recent HiGHS solve
        self.improved = True     # any change since then that could raise it
        self.needs_rebuild = True

    def build(self, longs, shorts):
        """(Re)build the model from a full leg set (static_arb._build_legs shape)."""
        legs = [(leg_key(l, "long"), l) for l in longs] + [(leg_key(s, "short"), s) for s in shorts]
        self.cols = {k: i for i, (k, _) in enumerate(legs)}
        self.static = {k: _static(l) for k, l in legs}
        self.by_code = {}
        for k, _ in legs:
            self.by_code.setdefault(k[2], set()).add(k)

        kinks = sorted({0.0} | {s[0] for s in self.static.values()})
        n = len(legs)
        A = np.zeros((len(kinks) + 1, n))
        for j, (k, leg) in enumerate(legs):
            strike, is_call, _ = self.static[k]
            pts = np.asarray(kinks)
            pay = np.maximum(0.0, pts - strike) if is_call else np.maximum(0.0, strike - pts)
            sign = -1.0 if k[0] == "long" else 1.0
            A[:-1, j] = sign * pay
            A[-1, j] = sign * (1.0 if is_call else 0.0)
        cost = np.array([(1.0 if k[0] == "long" else -1.0) * leg["price_ps"] for k, leg in legs])
        upper = np.array([float(leg["depth_shares"]) for _, leg in legs])

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        lp = highspy.HighsLp()
        lp.num_col_ = n
        lp.num_row_ = A.shape[0]
        lp.col_cost_ = cost
        lp.col_lower_ = np.zeros(n)
        lp.col_upper_ = upper
        lp.row_lower_ = np.full(A.shape[0], -highspy.kHighsInf)
        lp.row_upper_ = np.zeros(A.shape[0])
        csc = A.T  # column-major: one row of A.T per column
        starts, index, value = [0], [], []
        for j in range(n):
            nz = np.nonzero(csc[j])[0]
            index.extend(nz.tolist())
            value.extend(csc[j][nz].tolist())
            starts.append(len(index))
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = starts
        lp.a_matrix_.index_ = index
        lp.a_matrix_.value_ = value
        h.passModel(lp)
        self.h = h
        self.cost, self.upper = cost, upper
        self.last_relax, self.improved = None, True
        self.needs_rebuild = False

    def apply_instrument(self, code, longs, shorts):
        """Push one instrument's current legs (static_arb._build_legs output for just
        that instrument at this horizon); flags a rebuild if its leg set's terms changed."""
        if self.h is None or self.needs_rebuild:
            self.needs_rebuild = True
            return
        present = set()
        for side, legs in (("long", longs), ("short", shorts)):
            for leg in legs:
                k = leg_key(leg, side)
                if k not in self.cols or self.static[k] != _static(leg):
                    self.needs_rebuild = True
                    return
                present.add(k)
                self._set(self.cols[k], (1.0 if side == "long" else -1.0) * leg["price_ps"],
                          float(leg["depth_shares"]))
        for k in self.by_code.get(code, ()):
            if k not in present:   # quote pulled: the leg can't be traded right now
                self._set(self.cols[k], self.cost[self.cols[k]], 0.0)

    def _set(self, j, cost, upper):
        """Change one column, noting whether the change could raise the relaxation."""
        old_cost, old_upper = self.cost[j], self.upper[j]
        if cost == old_cost and upper == old_upper:
            return
        if cost < old_cost or (upper > old_upper and (old_upper == 0.0 or self.last_relax != 0.0)):
            self.improved = True
        if cost != old_cost:
            self.h.changeColCost(j, cost)
            self.cost[j] = cost
        if upper != old_upper:
            self.h.changeColBounds(j, 0.0, upper)
            self.upper[j] = upper

    def relaxation(self):
        """Max entry credit of the continuous LP (NT$); +inf if the solve fails (forces a full check)."""
        self.h.run()
        if self.h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            self.last_relax, self.improved = None, True
            return float("inf")
        self.last_relax = -self.h.getInfo().objective_function_value
        self.improved = False
        return self.last_relax

    def must_full_solve(self, min_edge=0.0):
        """True when the full static_arb solver could find an arb; None when no solve was needed."""
        threshold = max(min_edge, static_arb._TOL) * SCREEN_FRACTION
        if self.last_relax is not None and self.last_relax <= threshold and not self.improved:
            return None
        return self.relaxation() > threshold
