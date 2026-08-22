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


def direct_pairs(w_type, w_is_put, w_strike, w_dte, w_ratio, w_ask, w_bid,
                 o_type, o_strike, o_dte, o_bid, o_ask, o_bid_live, o_ask_live,
                 max_dte_diff, positive_loose):
    """Every warrant/option pair the same-type matcher would emit, in order.

    The candidate scan and both price-difference branches; the caller applies the
    (warrant_code, contract) dedup and builds the rows. `w_type` / `o_type` are
    integer codes for the same "Call"/"Put" strings the matcher compared, so the
    type match happens inside the loop and emission order stays exactly what the
    per-row code produced: direction, then warrant, then option.

    Returns `(wi, oi, direction, price_diff, exec_opt, exec_warrant,
    strike_diff_pct, dte_diff, favorable, max_loss_per_share)` per pair, where
    `direction` is 0 for positive (buy warrant / sell option) and 1 for negative.
    NaN quotes propagate exactly as they did per row: `NaN <= 0` is False, so a
    pair priced off a missing side still reaches the caller.
    """
    out = []
    nw = len(w_strike)
    no = len(o_strike)
    if nw == 0 or no == 0:
        return out

    for direction in (0, 1):
        positive = direction == 0
        for wi in range(nw):
            Kw = w_strike[wi]
            dte_w = w_dte[wi]
            is_put_w = w_is_put[wi]
            ratio = w_ratio[wi]
            if ratio <= 0:
                continue  # can't size or normalise a warrant with no exercise ratio

            # The residual vertical must be FAVORABLE (payoff >= 0 everywhere) so
            # the entry credit is never clawed back at expiry.
            opt_ge_warrant = positive != is_put_w
            ask_ps = round(w_ask[wi] / ratio, 4)
            bid_live = w_bid[wi] > 0
            # Loose marks tolerate the ask-as-bid fallback; an EXECUTABLE sell
            # does not — nobody lifts a bid that isn't there.
            bid_ps = round((w_bid[wi] if bid_live else w_ask[wi]) / ratio, 4)
            bid_real_ps = round(w_bid[wi] / ratio, 4) if bid_live else None

            typ_w = w_type[wi]
            for oi in range(no):
                if o_type[oi] != typ_w:
                    continue
                Ko = o_strike[oi]
                favorable = (Ko >= Kw) if opt_ge_warrant else (Ko <= Kw)
                if not favorable:
                    continue
                dte_o = o_dte[oi]
                # Never hold the SHORT leg as the longer-dated one — the hedge
                # leg would expire first, leaving a naked short position.
                if positive:
                    if dte_o > dte_w:
                        continue
                elif max(dte_w - dte_o, 0) > max_dte_diff:
                    continue

                ask_live = o_ask_live[oi]
                bid_live_o = o_bid_live[oi]
                if not (ask_live or bid_live_o):
                    continue
                o_bid_ps = round(o_bid[oi], 4) if bid_live_o else None
                o_ask_ps = round(o_ask[oi], 4) if ask_live else None

                # Tight = prices you can actually hit. Loose = optimistic
                # favorable-side mark, for ranking only.
                if positive:
                    if positive_loose:
                        if o_ask_ps is None:
                            continue
                        price_diff = round(o_ask_ps - bid_ps, 4)
                        exec_opt, exec_warrant = o_ask_ps, bid_ps
                    else:
                        if o_bid_ps is None:
                            continue
                        price_diff = round(o_bid_ps - ask_ps, 4)
                        exec_opt, exec_warrant = o_bid_ps, ask_ps
                    if price_diff <= 0:
                        continue
                else:
                    if positive_loose:
                        if o_bid_ps is None:
                            continue
                        price_diff = round(o_bid_ps - ask_ps, 4)
                        exec_opt, exec_warrant = o_bid_ps, ask_ps
                    else:
                        # A synthetic (ask-as-bid) warrant bid would fake the
                        # proceeds, so demand the REAL one.
                        if o_ask_ps is None or bid_real_ps is None:
                            continue
                        price_diff = round(o_ask_ps - bid_real_ps, 4)
                        exec_opt, exec_warrant = o_ask_ps, bid_real_ps
                    if price_diff >= 0:
                        continue

                out.append((
                    wi, oi, direction, price_diff, exec_opt, exec_warrant,
                    abs(Ko - Kw) / Kw * 100, abs(dte_o - dte_w),
                    favorable, 0.0 if favorable else abs(Ko - Kw),
                ))
    return out
