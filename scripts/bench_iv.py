"""Benchmark the Rust IV engine against the pure-Python reference.

Run: `python scripts/bench_iv.py [--rows 1500] [--reps 5]`. Reports wall time for
the vectorized IV solve at several batch sizes, the scalar solve, and a full
`build_warrant_df` pass (the warrant scanner's whole compute stage).
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from logic import bs_python, iv_engine  # noqa: E402


def make_batch(n, seed=42):
    """Scanner-shaped inputs: strikes clustered near spot, warrant exercise ratios, real DTEs."""
    rng = np.random.default_rng(seed)
    S = rng.uniform(20.0, 1200.0, n)
    K = S * rng.uniform(0.75, 1.35, n)
    T = rng.uniform(7.0, 300.0, n) / 365.0
    r = np.full(n, 0.02)
    ratio = rng.choice([0.05, 0.1, 0.25, 0.5, 1.0], n)
    is_put = rng.random(n) < 0.5
    sigma = rng.uniform(0.15, 0.9, n)
    price = np.array([
        bs_python.bs_price(S[i], K[i], T[i], r[i], sigma[i], ratio[i], bool(is_put[i]))
        for i in range(n)
    ])
    return price, S, K, T, r, ratio, is_put


def timeit(fn, reps):
    fn()  # warm up
    t0 = time.perf_counter()
    for _ in range(reps):
        fn()
    return (time.perf_counter() - t0) / reps


def bench_vec(sizes, reps):
    print(f"{'rows':>8}  {'python':>12}  {'rust':>10}  {'speedup':>9}")
    for n in sizes:
        args = make_batch(n)
        r = max(1, reps if n <= 20000 else 2)
        tp = timeit(lambda: bs_python.implied_vol_vec(*args), r)
        tr = timeit(lambda: iv_engine.implied_vol_vec(*args), r)
        print(f"{n:>8}  {tp * 1e3:>9.2f} ms  {tr * 1e3:>7.3f} ms  {tp / tr:>8.1f}x")


def bench_scalar(reps=3000):
    S, K, T, r, ratio = 620.0, 640.0, 75 / 365.0, 0.02, 0.1
    price = bs_python.bs_price(S, K, T, r, 0.42, ratio, False)
    tp = timeit(lambda: bs_python.implied_vol(price, S, K, T, r, ratio, False), reps)
    tr = timeit(lambda: iv_engine.implied_vol(price, S, K, T, r, ratio, False), reps)
    print(f"\nsingle IV solve   python {tp * 1e6:8.2f} us   rust {tr * 1e6:7.3f} us   "
          f"speedup {tp / tr:6.1f}x")


def bench_frame(rows, reps):
    """Full warrant-scanner compute stage: parse -> IV/delta/leverage solve -> DataFrame."""
    from logic import warrant_logic

    rng = np.random.default_rng(99)
    data = {}
    for i in range(rows):
        S = float(rng.uniform(20, 900))
        K = float(S * rng.uniform(0.75, 1.35))
        dte = int(rng.integers(7, 300))
        ratio = float(rng.choice([0.05, 0.1, 0.25, 0.5, 1.0]))
        is_put = bool(rng.random() < 0.5)
        ask = bs_python.bs_price(S, K, dte / 365.0, 0.02, float(rng.uniform(0.15, 0.9)),
                                 ratio, is_put)
        data[f"0{i:05d}"] = {
            "Warrant": {"SellPr1": round(ask, 2), "BuyPr1": round(ask * 0.97, 2),
                        "SellQty1": 10, "BuyQty1": 8, "SaleQty": 100,
                        "CommName": f"W{i}", "LastDays": dte, "StrikePr": K,
                        "UserRate": ratio, "CallorPut": 2 if is_put else 1},
            "Stock": {"CommKey": "2330", "SalePr": S},
        }

    saved = {k: getattr(warrant_logic, k) for k in
             ("implied_vol", "implied_vol_vec", "bs_delta_vec", "_refine_iv_for_rounding")}
    tr = timeit(lambda: warrant_logic.build_warrant_df(data), reps)
    try:
        for k in saved:
            setattr(warrant_logic, k, getattr(bs_python, k))
        tp = timeit(lambda: warrant_logic.build_warrant_df(data), max(1, reps // 2))
    finally:
        for k, v in saved.items():
            setattr(warrant_logic, k, v)
    print(f"\nbuild_warrant_df({rows} warrants)   python {tp * 1e3:8.1f} ms   "
          f"rust {tr * 1e3:7.2f} ms   speedup {tp / tr:6.1f}x")


def bench_surface(points=1200, reps=5):
    """The IV-surface stage that is NOT IV: scipy.griddata onto the 80x80 grid."""
    from logic import iv_surface

    rng = np.random.default_rng(7)
    x = rng.uniform(300, 900, points)
    y = rng.uniform(7, 300, points)
    z = rng.uniform(0.2, 0.9, points) * 100
    t = timeit(lambda: iv_surface.interpolate_grid(x, y, z, 80), reps)
    print(f"\ninterpolate_grid({points} pts -> 80x80)   {t * 1e3:8.2f} ms  "
          f"(scipy griddata; unchanged by the engine swap)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1500, help="warrants for the frame benchmark")
    ap.add_argument("--reps", type=int, default=5)
    a = ap.parse_args()
    print(f"engine: {iv_engine.engine_info()}\n")
    bench_vec([100, 500, 1500, 3000, 10000, 50000], a.reps)
    bench_scalar()
    bench_frame(a.rows, a.reps)
    bench_surface()
