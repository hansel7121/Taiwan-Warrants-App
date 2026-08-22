"""Pure-Python reference for the arb matcher kernels.

These are the fallback half of `logic/iv_engine.py`'s `arb` feature: same
signatures, same return shapes, same values as the Rust kernels in
`rust/warrants_core/src/arb.rs`. Plain arrays and tuples in and out — no pandas,
no pyo3 types — which is what lets either engine be dropped in and lets each
kernel be benchmarked on its own.

Kept as the reference, not as dead code: `RUST_ENGINE=python` runs the whole app
through here, and the parity tests compare the two engines row for row.
"""
from bisect import bisect_left, bisect_right


def butterfly_pairs(wing_k, wing_buy, wing_dte,
                    body_k, body_sell, body_dte, body_orig, is_call):
    """Every wing pair whose best body yields a locked-in credit.

    Wings arrive sorted by strike; bodies arrive sorted by strike carrying their
    original chain position in `body_orig`, because a tie on `sell_ps` keeps the
    body that came first in the chain.

    Returns `(wing_lo, wing_hi, body_orig_idx, credit_ps, tail, worst_payoff_ps,
    guaranteed_ps)` per surviving pair, in wing-pair order.
    """
    out = []
    nw = len(wing_k)
    if nw < 2 or not len(body_k):
        return out
    ks = list(body_k)

    for a in range(nw):
        k1 = wing_k[a]
        lo = bisect_right(ks, k1)
        for b in range(a + 1, nw):
            k2 = wing_k[b]
            hi = bisect_left(ks, k2)
            if lo >= hi:
                continue
            dte_cap = min(wing_dte[a], wing_dte[b])

            best = -1
            for i in range(lo, hi):
                if body_dte[i] > dte_cap:
                    continue
                if best < 0:
                    best = i
                    continue
                cur = body_sell[best]
                if body_sell[i] > cur or (body_sell[i] == cur
                                          and body_orig[i] < body_orig[best]):
                    best = i
            if best < 0:
                continue

            x = body_k[best]
            credit_ps = round(2 * body_sell[best] - wing_buy[a] - wing_buy[b], 6)
            tail = (2 * x - k1 - k2) if is_call else (k1 + k2 - 2 * x)
            worst_payoff_ps = min(0.0, tail)
            guaranteed_ps = round(worst_payoff_ps + credit_ps, 6)
            if guaranteed_ps <= 0:
                continue  # not a locked arb — skip
            out.append((a, b, int(body_orig[best]), credit_ps, float(tail),
                        worst_payoff_ps, guaranteed_ps))
    return out
