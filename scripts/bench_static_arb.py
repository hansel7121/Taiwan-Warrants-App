"""Benchmark + parity harness for the static-arb LP (Live Arb LP subtab kernel).

Generates a reproducible TSMC-scale warrant/option book, then runs the same
per-horizon scan through every available implementation (scipy/HiGHS reference
in `logic/static_arb.py`, the Rust `solve_static_arb_horizon` kernel, and the
batched `scan_static_arb` entry point) and reports wall-clock per scan plus a
field-by-field diff of what each one found. Run it before and after any change
to either implementation.

    python scripts/bench_static_arb.py                 # default scale
    python scripts/bench_static_arb.py --warrants 1200 --seed 7 --repeat 20
"""
import argparse
import math
import random
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import static_arb  # noqa: E402

M = 2000.0
R = 0.01875


# ── synthetic book ───────────────────────────────────────────────────────────

def _bs(s, k, t, r, sig, put=False):
    """Black-Scholes price — the arbitrage-free anchor the book is priced off."""
    if t <= 0:
        return max(0.0, (k - s) if put else (s - k))
    from scipy.stats import norm
    d1 = (math.log(s / k) + (r + sig * sig / 2) * t) / (sig * math.sqrt(t))
    d2 = d1 - sig * math.sqrt(t)
    if put:
        return k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)
    return s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2)


def make_book(n_warrants=800, n_expiries=5, strikes_per_expiry=21, seed=0,
              spot=1000.0, mispricing=0.02):
    """A TSMC-scale book: `n_warrants` warrants across many strikes/expiries,
    plus a TAIFEX-style option chain. Quotes are BS +- a random spread, with
    `mispricing` worth of noise so some horizons really do carry a structure."""
    rng = random.Random(seed)
    expiries = [17, 45, 80, 108, 199][:n_expiries]
    if n_expiries > 5:
        expiries = sorted({17, 45, 80, 108, 199} | {30 + 37 * i for i in range(n_expiries - 5)})

    warrants = []
    for i in range(n_warrants):
        typ = "Call" if rng.random() < 0.78 else "Put"
        k = round(spot * rng.uniform(0.68, 1.42) / 5.0) * 5.0
        dte = rng.choice([9, 21, 33, 45, 58, 72, 86, 101, 120, 145, 170, 199, 240, 300])
        ratio = rng.choice([0.05, 0.1, 0.125, 0.2, 0.25, 0.5, 1.0])
        sig = rng.uniform(0.24, 0.44)
        fair = _bs(spot, k, dte / 365.0, R, sig, typ == "Put")
        ask = max(0.01, fair * (1.0 + rng.uniform(-mispricing, mispricing * 2))) * ratio
        warrants.append({
            "warrant_code": f"W{i:05d}", "warrant_name": f"warrant{i}",
            "underlying_code": "2330", "type": typ, "underlying_price": spot,
            "ask": round(ask, 2), "bid": round(ask * 0.97, 2),
            "ask_qty": rng.randint(1, 60), "bid_qty": rng.randint(1, 60),
            "days_to_expiry": dte, "strike": k, "exercise_ratio": ratio,
            "volume": rng.randint(0, 5000),
        })

    options = []
    for dte in expiries:
        base = round(spot / 25.0) * 25.0
        for j in range(strikes_per_expiry):
            k = base + (j - strikes_per_expiry // 2) * 25.0
            if k <= 0:
                continue
            for typ in ("Call", "Put"):
                sig = rng.uniform(0.26, 0.40)
                fair = _bs(spot, k, dte / 365.0, R, sig, typ == "Put")
                half = max(0.5, fair * rng.uniform(0.01, 0.05))
                bid = max(0.0, fair * (1.0 + rng.uniform(-mispricing, mispricing)) - half)
                ask = bid + 2 * half
                options.append({
                    "contract": f"O{typ[0]}{int(k)}D{dte}", "stock_code": "2330",
                    "type": typ, "underlying_price": spot, "strike": k,
                    "days_to_expiry": dte, "bid": round(bid, 2), "ask": round(ask, 2),
                    "bid_size": rng.randint(1, 40), "ask_size": rng.randint(1, 40),
                    "exercise_ratio": M, "volume": rng.randint(0, 900), "oi": 500,
                    "bid_live": bid > 0, "ask_live": True,
                })

    return pd.DataFrame(warrants), pd.DataFrame(options)


# ── one full scan, per implementation ────────────────────────────────────────

def _row_summary(row, T_star):
    """The fields any implementation must agree on, for a per-horizon diff."""
    if row is None:
        return None
    legs = sorted((l["side"], l["code"], l["lots"]) for l in row["legs"])
    return {
        "horizon": T_star,
        "net_credit": round(float(row["net_credit"]), 4),
        "min_payoff": round(float(row["min_payoff"]), 4),
        "guaranteed_profit": round(float(row["guaranteed_profit"]), 4),
        "gross_debit": round(float(row["gross_debit"]), 4),
        "worst_spot": round(float(row["worst_spot"]), 4),
        "legs": legs,
    }


def scan_python(wdf, odf, min_edge=0.0, r=R):
    """The scipy/HiGHS reference: build legs + solve, per horizon, in Python."""
    out = []
    for T_star in sorted({int(d) for d in odf["days_to_expiry"].unique()}):
        longs, shorts, _ = static_arb._build_legs(wdf, odf, T_star, M, r)
        row = static_arb._solve_horizon(longs, shorts, T_star, min_edge)
        if row:
            out.append(_row_summary(row, T_star))
    return out


def scan_rust_per_horizon(wdf, odf, min_edge=0.0, r=R):
    """Python leg-building + the Rust per-horizon kernel (the shipped path)."""
    from logic import iv_engine
    out = []
    for T_star in sorted({int(d) for d in odf["days_to_expiry"].unique()}):
        longs, shorts, _ = static_arb._build_legs(wdf, odf, T_star, M, r)
        if not longs or not shorts:
            continue
        res = iv_engine.solve_static_arb_horizon(
            [l["price_ps"] for l in longs], [l["eff_strike"] for l in longs],
            [l["is_call"] for l in longs], [l["lot_shares"] for l in longs],
            [l["depth_shares"] for l in longs],
            [s["price_ps"] for s in shorts], [s["eff_strike"] for s in shorts],
            [s["is_call"] for s in shorts], [s["lot_shares"] for s in shorts],
            [s["depth_shares"] for s in shorts],
            min_edge,
        )
        if res is None:
            continue
        (li, ll, si, sl, credit, min_payoff, guaranteed, worst_spot, gross_debit) = res
        legs = sorted([("long", longs[i]["code"], int(n)) for i, n in zip(li, ll)]
                      + [("short", shorts[i]["code"], int(n)) for i, n in zip(si, sl)])
        out.append({
            "horizon": T_star, "net_credit": round(credit, 4),
            "min_payoff": round(min_payoff, 4), "guaranteed_profit": round(guaranteed, 4),
            "gross_debit": round(gross_debit, 4), "worst_spot": round(worst_spot, 4),
            "legs": legs,
        })
    return out


def scan_batched(wdf, odf, min_edge=0.0, r=R):
    """Leg-building and every horizon in one Rust call — the shipped fast path."""
    horizons = sorted({int(d) for d in odf["days_to_expiry"].unique()})
    rows, _ = static_arb._scan_chain(wdf, odf, horizons, M, r, min_edge)
    return [_row_summary(row, row["horizon_dte"]) for row in rows]


IMPLS = {"python": scan_python, "rust": scan_rust_per_horizon, "batched": scan_batched}


# ── diff + timing ────────────────────────────────────────────────────────────

def diff(ref, other, ref_name, other_name):
    """Report every horizon where two implementations disagree."""
    by_h_ref = {r["horizon"]: r for r in ref}
    by_h_other = {r["horizon"]: r for r in other}
    problems = []
    for h in sorted(set(by_h_ref) | set(by_h_other)):
        a, b = by_h_ref.get(h), by_h_other.get(h)
        if a is None or b is None:
            problems.append(f"  h={h}: {ref_name}={'row' if a else 'none'} "
                            f"{other_name}={'row' if b else 'none'}")
            continue
        for f in ("net_credit", "min_payoff", "guaranteed_profit", "gross_debit", "worst_spot"):
            if abs(a[f] - b[f]) > 0.51:
                problems.append(f"  h={h} {f}: {ref_name}={a[f]} {other_name}={b[f]}")
        if a["legs"] != b["legs"]:
            problems.append(f"  h={h} legs differ ({len(a['legs'])} vs {len(b['legs'])}) "
                            f"[tie-break, same value = OK]")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warrants", type=int, default=800)
    ap.add_argument("--expiries", type=int, default=5)
    ap.add_argument("--strikes", type=int, default=21)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--only", default=None, help="comma-separated impl names")
    args = ap.parse_args()

    wdf, odf = make_book(args.warrants, args.expiries, args.strikes, args.seed)
    horizons = sorted({int(d) for d in odf["days_to_expiry"].unique()})
    print(f"book: {len(wdf)} warrants, {len(odf)} option quotes, {len(horizons)} horizons "
          f"{horizons}")

    names = args.only.split(",") if args.only else list(IMPLS)
    results, timings = {}, {}
    for name in names:
        fn = IMPLS[name]
        fn(wdf, odf)                                  # warm up
        ts = []
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            rows = fn(wdf, odf)
            ts.append((time.perf_counter() - t0) * 1000)
        results[name], timings[name] = rows, ts
        print(f"{name:>8}: {statistics.median(ts):8.2f} ms median "
              f"(min {min(ts):7.2f}, max {max(ts):7.2f})   {len(rows)} structures")

    ref_name = names[0]
    for name in names[1:]:
        problems = diff(results[ref_name], results[name], ref_name, name)
        if problems:
            print(f"\nDIFF {ref_name} vs {name}:")
            print("\n".join(problems))
        else:
            print(f"\nDIFF {ref_name} vs {name}: identical")


if __name__ == "__main__":
    main()
