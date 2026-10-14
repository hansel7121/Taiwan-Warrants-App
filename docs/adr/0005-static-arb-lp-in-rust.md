---
status: accepted
---

# The static-arb LP in Rust: row-and-column generation, not a faster solver

Revises ADR-0004's "not done, deliberately" entry for `logic/static_arb.py`. That entry is still right about *why* the port is awkward — the LP is structurally degenerate, so the optimal value is unique but the vertex attaining it is not, and the vertex is what the tab displays. It was wrong that this made the port not worth doing.

**Why now:** the tick replay notebook (`notebooks/lp_arb_tick_replay.ipynb`) re-solves the whole chain after every recorded tick. At ~500 ms a tick, a 100k-tick session is two weeks of compute. The Live Arb LP subtab has the same shape at a lower rate. Both wanted a whole-chain scan under 10 ms.

## The LP was never the size it looked

One horizon of a TSMC-scale chain has ~1,000 buyable legs whose discounted strikes give ~700 distinct kinks, so `_solve_lp` hands scipy a dense 700×1,000 matrix. The optimum is supported on 24–67 legs. Everything else is a column the solver carries and never uses.

So the port is not "the same LP, in Rust". It is a smaller LP:

- **Columns are generated.** Seed with the short side (one expiry's bid quotes — small, and the entire objective), solve, read the row prices back out, and pull in only the legs those prices say are mispriced. Pricing every remaining leg is O(log m) each off prefix sums of the kink prices, so a round over 1,000 legs is microseconds.
- **Rows are generated too.** A piecewise-linear payoff is >= 0 everywhere iff it is >= 0 at S=0, at every kink, and in the far-right slope — and it only bends at the kinks of legs the incumbent actually holds. So a violated kink is separated the same way a violated column is.

At the round where neither separates anything, the incumbent is primal-feasible for the whole chain and the row prices extended by zero are dual-feasible for it: two matching certificates, so the restricted optimum is the full optimum. This is an exact reduction, not a heuristic — `tests/logic/test_static_arb_lp_parity.py` pins the relaxation's optimal credit against scipy's on seeded random chains, and it matches to 1e-7 relative.

The restricted problem settles at ~40 rows × ~150 columns, and the leg-building moved into Rust with it: `_build_legs` rebuilt every leg in the chain once per horizon and cost as much as all the LPs put together.

## Row generation is not optional, and the reason is numerical

The first working version gave every restricted column's own kink a row. That is tempting — it makes the incumbent feasible for the whole chain by construction, so row separation is never needed and the basis warm-starts perfectly across rounds.

It also leaves `rows ~= columns`, which forces nearly every column into the basis. An option chain is nearly rank-deficient: two warrants on one strike whose expiries differ by a week have payoff columns that agree to five figures. A basis holding all of them is singular in all but name. Measured `B^-1 A_q` at 3e15, at which point the ratio test computes a step of zero and the pivot moves a variable 54,550 shares clean across its bound. The solve returned answers that were not merely different from scipy's — they were wrong, by 1% of the objective, while reporting success.

Keeping the row set small (seeded with the short-side strikes, grown only by separation) gives the simplex a real choice of basis. That, plus refactorising `B^-1` from scratch on a fixed pivot cadence, a Harris two-pass ratio test so the leaving variable lands on the bound the test actually chose, and a Bland's-rule restart when a pivot still looks unsound, is what makes the answer exact rather than merely fast.

One repair was tried and rejected: holding a numerically awkward column out of the LP for the rest of the solve. It keeps the solve alive, but the duals of an LP solved without a column certify an optimum that is not one, and the generation loop then converges happily 1% short. A solver may restart, and it may report failure; it may not quietly drop a column.

## Why it needs its own simplex

The generation loop needs the dual vector after every solve — that is the price signal saying which of the thousand un-enumerated legs to add next. No general-purpose LP crate on crates.io hands that back; `microlp`, which the first Rust kernel used, does not. `rust/warrants_core/src/lp.rs` is a bounded-variable primal simplex specialised to this LP's shape: every row is `A z >= 0`, every variable is boxed in `[0, u]`, so `z = 0` is always feasible and there is no phase 1.

## What it costs, and what it still does not guarantee

On an 800-warrant, 210-quote, 5-horizon book: **500 ms to 7.4 ms**, identical rows including leg selection.

ADR-0004's caveat survives intact, and the numbers are now measured rather than feared. Over 60 random books (180 horizons), the finished rows are identical on 98.9%. On the rest the two solvers land on different vertices of a tied optimal face, lot rounding turns that into a different structure, and the Rust one is never the less profitable of the two. On a chain quoted at one flat vol — where the entire optimal face is tied — the fast path also finds a horizon the reference misses: a real conversion basket worth 380 NT$ that survives rounding from its vertex and not from scipy's.

So the bound the tests hold is one-sided: never fewer structures than the scipy path, never a worse one, and every structure independently verified riskless from its own legs. Bit-identical leg selection is not on offer from any two LP solvers on a degenerate problem, and pretending otherwise would mean pinning tests to something no implementation can promise.

`logic/static_arb.py` keeps the scipy loop as the fallback for a host without the extension, and as the reference the parity tests compare against. `RUST_ENGINE_OFF=arb` still selects it.
