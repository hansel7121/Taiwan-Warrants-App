"""The Rust static-arb LP against the scipy/HiGHS path it replaces.

`logic/static_arb.py`'s scipy loop is the reference, and three things are pinned
here, in descending order of how absolute they are:

1. The continuous relaxation's optimal credit must match. That number is unique
   even where the vertex attaining it is not, so a disagreement there is a bug,
   never a tie.
2. Every structure the fast path reports must be riskless on its own terms —
   payoff >= 0 at every kink, and the headline figures re-derivable from the
   legs. Checked without reference to either solver.
3. The finished rows must match the scipy loop's. This one has an escape: the
   two solvers can land on different vertices of a tied optimal face, and lot
   rounding turns that into a different (equally valid) structure. Measured at
   ~1% of horizons over 60 random books, always at equal or better guaranteed
   profit. On a chain quoted at one flat vol, where the whole optimal face is
   tied, the fast path also finds a horizon the reference misses — a real
   conversion basket worth 380 NT$ that survives rounding from its vertex and
   not from scipy's. So the bound is one-sided: never fewer structures than the
   reference, never a worse one, and everything extra has to verify.
"""
import math
import random

import pandas as pd
import pytest

from logic import iv_engine, static_arb
from tests.conftest import rust_only

M = 2000.0
R = 0.01875


def _bs(s, k, t, r, sig, put=False):
    """Black-Scholes, the arbitrage-free anchor the random books are priced off."""
    from scipy.stats import norm
    if t <= 0:
        return max(0.0, (k - s) if put else (s - k))
    d1 = (math.log(s / k) + (r + sig * sig / 2) * t) / (sig * math.sqrt(t))
    d2 = d1 - sig * math.sqrt(t)
    if put:
        return k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)
    return s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2)


def random_book(seed, n_warrants=90, n_expiries=3, strikes=9, spot=600.0,
                mispricing=0.02, one_vol=None):
    """A chain with an independent vol per quote, so real cross-instrument
    mispricings exist and the LP has something to find at most horizons. Pass
    `one_vol` with `mispricing=0` for the arbitrage-free case instead."""
    rng = random.Random(seed)
    vol = (lambda lo, hi: one_vol) if one_vol else rng.uniform
    expiries = sorted(rng.sample(range(10, 210), n_expiries))
    warrants = []
    for i in range(n_warrants):
        typ = "Call" if rng.random() < 0.75 else "Put"
        k = round(spot * rng.uniform(0.7, 1.4) / 5.0) * 5.0
        dte = rng.choice([7, 20, 35, 55, 80, 110, 150, 200, 260])
        ratio = rng.choice([0.05, 0.1, 0.2, 0.5, 1.0])
        fair = _bs(spot, k, dte / 365.0, R, vol(0.25, 0.45), typ == "Put")
        ask = max(0.01, fair * (1.0 + rng.uniform(-mispricing, 2 * mispricing))) * ratio
        warrants.append({
            "warrant_code": f"W{i:04d}", "warrant_name": f"w{i}",
            "underlying_code": "2330", "type": typ, "underlying_price": spot,
            "ask": round(ask, 2), "ask_qty": rng.randint(0, 40),
            "days_to_expiry": dte, "strike": k, "exercise_ratio": ratio,
            "volume": rng.randint(0, 900),
        })

    options = []
    for dte in expiries:
        base = round(spot / 25.0) * 25.0
        for j in range(strikes):
            k = base + (j - strikes // 2) * 25.0
            for typ in ("Call", "Put"):
                fair = _bs(spot, k, dte / 365.0, R, vol(0.26, 0.42), typ == "Put")
                half = 0.0 if one_vol else max(0.5, fair * rng.uniform(0.01, 0.06))
                bid = max(0.0, fair * (1.0 + rng.uniform(-mispricing, mispricing)) - half)
                options.append({
                    "contract": f"O{typ[0]}{int(k)}D{dte}", "stock_code": "2330",
                    "type": typ, "underlying_price": spot, "strike": k,
                    "days_to_expiry": dte, "bid": round(bid, 2),
                    "ask": round(bid + 2 * half, 2),
                    "bid_size": rng.randint(0, 30), "ask_size": rng.randint(0, 30),
                    "exercise_ratio": M, "volume": rng.randint(0, 500), "oi": 400,
                    "bid_live": bid > 0, "ask_live": True,
                })
    return pd.DataFrame(warrants), pd.DataFrame(options)


def horizons_of(opt_df):
    return sorted({int(d) for d in opt_df["days_to_expiry"].unique()})


def scipy_rows(wdf, odf):
    """The reference scan: build legs and solve per horizon, all in Python."""
    out, dropped = [], 0
    for t_star in horizons_of(odf):
        longs, shorts, drop = static_arb._build_legs(wdf, odf, t_star, M, R)
        dropped += drop
        row = static_arb._solve_horizon(longs, shorts, t_star, 0.0)
        if row:
            out.append(row)
    return out, dropped


def summarize(row):
    """The fields any implementation of the scan has to agree on."""
    return {
        "horizon_dte": row["horizon_dte"],
        "net_credit": row["net_credit"],
        "min_payoff": row["min_payoff"],
        "guaranteed_profit": row["guaranteed_profit"],
        "gross_debit": row["gross_debit"],
        "worst_spot": row["worst_spot"],
        "return_pct": row["return_pct"],
        "legs": sorted((l["side"], l["code"], l["lots"]) for l in row["legs"]),
    }


def verify(row):
    """Re-derive one row's claim from its own legs, with no solver involved.

    Returns (min_payoff, net_credit, tol) recomputed from the leg list: the
    payoff of the whole basket at every spot where it can bend, the entry cash
    flow, and how far off those may legitimately land. The legs carry display
    values (`price_ps` to 4 decimals, `eff_strike` to 4), so a 100,000-share
    position re-adds to within a few NT$ of the row's own figures and no closer.
    """
    kinks = sorted({0.0} | {round(l["eff_strike"], 6) for l in row["legs"]})
    worst = math.inf
    for s in kinks:
        total = 0.0
        for leg in row["legs"]:
            k = leg["eff_strike"]
            pay = max(0.0, s - k) if leg["type"] == "Call" else max(0.0, k - s)
            total += (pay * leg["shares"]) * (1 if leg["side"] == "long" else -1)
        worst = min(worst, total)
    credit = math.fsum(l["price_ps"] * l["shares"] * (1 if l["side"] == "short" else -1)
                       for l in row["legs"])
    tol = 1.0 + 1e-4 * sum(l["shares"] for l in row["legs"])
    return worst, credit, tol


# ── the continuous relaxation ────────────────────────────────────────────────

@rust_only
@pytest.mark.parametrize("seed", range(12))
def test_relaxation_credit_matches_scipy(seed):
    """The LP optimum is unique even when the vertex attaining it is not, so
    this is the assertion that admits no tie-break excuse."""
    import warrants_core

    wdf, odf = random_book(seed)
    checked = 0
    for t_star in horizons_of(odf):
        longs, shorts, _ = static_arb._build_legs(wdf, odf, t_star, M, R)
        if not longs or not shorts:
            continue
        res, _ = static_arb._solve_lp(longs, shorts)
        assert res.success
        credit, _, _ = warrants_core.static_arb_relaxation(
            [l["price_ps"] for l in longs], [l["eff_strike"] for l in longs],
            [l["is_call"] for l in longs], [l["lot_shares"] for l in longs],
            [l["depth_shares"] for l in longs],
            [s["price_ps"] for s in shorts], [s["eff_strike"] for s in shorts],
            [s["is_call"] for s in shorts], [s["lot_shares"] for s in shorts],
            [s["depth_shares"] for s in shorts],
        )
        scipy_credit = -float(res.fun)
        assert credit == pytest.approx(scipy_credit, rel=1e-7, abs=1e-6), (
            f"seed={seed} horizon={t_star}: scipy={scipy_credit} rust={credit}")
        checked += 1
    assert checked, "the generated book produced no solvable horizon"


# ── every reported structure is riskless on its own terms ────────────────────

@rust_only
@pytest.mark.parametrize("seed", range(20))
def test_reported_structures_are_riskless(seed):
    wdf, odf = random_book(seed)
    rows, _ = static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)
    for row in rows:
        worst, credit, tol = verify(row)
        where = f"seed={seed} h={row['horizon_dte']}"
        assert worst >= -tol, f"{where}: the basket pays {worst} at some spot"
        assert credit > 0, f"{where}: the entry is a debit, not a credit"
        assert credit == pytest.approx(row["net_credit"], abs=tol), where
        assert worst == pytest.approx(row["min_payoff"], abs=tol), where
        assert row["guaranteed_profit"] == pytest.approx(
            row["net_credit"] + max(0.0, row["min_payoff"]), abs=1.0), where


# ── the finished rows against the scipy loop ─────────────────────────────────

@rust_only
@pytest.mark.parametrize("seed", range(12))
def test_batched_scan_matches_the_scipy_loop(seed):
    """Same horizons, same rows — except where a tied optimal face lets the two
    solvers pick different vertices, which lot rounding then turns into a
    different structure. Those must still be at least as profitable."""
    wdf, odf = random_book(seed)
    expected, dropped_expected = scipy_rows(wdf, odf)
    rows, dropped = static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)

    assert dropped == dropped_expected, f"seed={seed}"
    by_horizon = {r["horizon_dte"]: r for r in rows}
    for want in expected:
        got = by_horizon.get(want["horizon_dte"])
        assert got is not None, (
            f"seed={seed} h={want['horizon_dte']}: the reference found a structure "
            "here and the fast path did not")
        if summarize(got) == summarize(want):
            continue
        assert got["guaranteed_profit"] >= want["guaranteed_profit"], (
            f"seed={seed} h={got['horizon_dte']}: a different structure at LOWER "
            f"profit ({got['guaranteed_profit']} vs {want['guaranteed_profit']}) "
            "is a regression, not a tie-break")


@rust_only
def test_rows_match_the_scipy_loop_on_almost_every_horizon():
    """The aggregate the per-seed test cannot make: tied vertices are rare, and
    a change that makes them common should fail here even though every
    individual structure still verifies."""
    identical = total = 0
    for seed in range(30):
        wdf, odf = random_book(seed)
        expected = {r["horizon_dte"]: summarize(r) for r in scipy_rows(wdf, odf)[0]}
        rows, _ = static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)
        for row in rows:
            total += 1
            identical += summarize(row) == expected.get(row["horizon_dte"])
    assert total >= 50, "the fixture books stopped producing structures"
    assert identical / total >= 0.95, f"only {identical}/{total} horizons matched exactly"


@rust_only
@pytest.mark.parametrize("seed", range(6))
def test_per_horizon_kernel_matches_the_batched_scan(seed):
    """The column-array entry point `logic/live_arb_lp_logic.py` calls, on legs
    the caller built itself — same kernel, so this one is exact."""
    wdf, odf = random_book(seed)
    batched = {r["horizon_dte"]: summarize(r)
               for r in static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)[0]}
    for t_star in horizons_of(odf):
        longs, shorts, _ = static_arb._build_legs(wdf, odf, t_star, M, R)
        if not longs or not shorts:
            assert t_star not in batched
            continue
        got = iv_engine.solve_static_arb_horizon(
            [l["price_ps"] for l in longs], [l["eff_strike"] for l in longs],
            [l["is_call"] for l in longs], [l["lot_shares"] for l in longs],
            [l["depth_shares"] for l in longs],
            [s["price_ps"] for s in shorts], [s["eff_strike"] for s in shorts],
            [s["is_call"] for s in shorts], [s["lot_shares"] for s in shorts],
            [s["depth_shares"] for s in shorts],
            0.0,
        )
        want = batched.get(t_star)
        if want is None:
            assert got is None, f"seed={seed} horizon={t_star}"
            continue
        assert got is not None, f"seed={seed} horizon={t_star}"
        long_idx, long_lots, short_idx, short_lots = got[:4]
        credit, min_payoff, guaranteed, worst_spot, gross_debit = got[4:]
        assert (credit, min_payoff, guaranteed, worst_spot, gross_debit) == (
            want["net_credit"], want["min_payoff"], want["guaranteed_profit"],
            want["worst_spot"], want["gross_debit"])
        legs = sorted([("long", longs[i]["code"], int(n)) for i, n in zip(long_idx, long_lots)]
                      + [("short", shorts[j]["code"], int(n)) for j, n in zip(short_idx, short_lots)])
        assert legs == want["legs"]


# ── the shapes the random books do not reach ─────────────────────────────────

@rust_only
def test_one_vol_chain_agrees_with_scipy_on_what_is_there():
    """One vol across the whole chain and zero option spread — every quote
    consistent with every other, so what little the LP can still find (warrant
    spread over fair, the American put floor) is found on a completely tied
    optimal face. Leg-for-leg agreement is not meaningful here; agreeing on
    which horizons pay, and how much, is."""
    wdf, odf = random_book(0, mispricing=0.0, one_vol=0.35)
    expected, _ = scipy_rows(wdf, odf)
    rows, _ = static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)
    by_horizon = {r["horizon_dte"]: r for r in rows}
    for want in expected:
        got = by_horizon.get(want["horizon_dte"])
        assert got is not None, f"h={want['horizon_dte']} lost"
        assert got["guaranteed_profit"] >= want["guaranteed_profit"]
    for got in rows:
        worst, credit, tol = verify(got)
        assert worst >= -tol, f"h={got['horizon_dte']} pays {worst}"
        assert credit > 0


@rust_only
def test_scan_without_warrants_still_matches():
    """Options can be bought as well as sold, so an empty warrant frame is a
    thinner chain, not an empty one."""
    wdf, odf = random_book(0)
    wdf = wdf.iloc[0:0]
    expected, _ = scipy_rows(wdf, odf)
    rows, _ = static_arb._scan_chain(wdf, odf, horizons_of(odf), M, R, 0.0)
    found = {r["horizon_dte"] for r in rows}
    assert found >= {r["horizon_dte"] for r in expected}
    for row in rows:
        worst, _, tol = verify(row)
        assert worst >= -tol


@rust_only
def test_scan_without_options_is_empty():
    """Shorts are options only, so no option chain means no structure — and no
    horizons to scan in the first place."""
    wdf, odf = random_book(0)
    assert static_arb._scan_chain(wdf, odf.iloc[0:0], [], M, R, 0.0) == ([], 0)


@rust_only
def test_min_edge_floor_is_respected():
    """Raising the floor can only remove rows, never change the ones that stay."""
    wdf, odf = random_book(3)
    horizons = horizons_of(odf)
    base, _ = static_arb._scan_chain(wdf, odf, horizons, M, R, 0.0)
    assert base, "the fixture book must find something for this to test anything"
    floor = max(r["guaranteed_profit"] for r in base) / 2
    raised, _ = static_arb._scan_chain(wdf, odf, horizons, M, R, floor)
    assert len(raised) <= len(base)
    for row in raised:
        assert row["guaranteed_profit"] > floor
        assert summarize(row) in [summarize(b) for b in base]
