"""Time every ported code path against the committed fixtures.

Fixture-driven rather than live, so the numbers are reproducible months later
and the input size is always known — everything in arb_logic is O(W x O), which
makes a bare "8x faster" meaningless without it.

Reported single-threaded: the deploy is a 1-vCPU Render standard instance, so a
rayon-parallel number would flatter the local machine and mean nothing there.

    python scripts/bench_engines.py                 # every fixture scenario
    python scripts/bench_engines.py --only pcp      # substring filter
    python scripts/bench_engines.py --json out.json # for a before/after diff
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logic import iv_engine  # noqa: E402
from tests.logic import arb_golden  # noqa: E402


def median_ms(fn, reps):
    fn()  # warm up: first call pays import and cache costs the rest do not
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts) * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--only", help="substring filter on the scenario name")
    ap.add_argument("--json", help="write the results here for a before/after diff")
    args = ap.parse_args()

    print(f"engine: {iv_engine.engine_info()}\n")
    print(f"{'scenario':<24}{'matcher':<15}{'input':<24}{'rows':>6}{'median ms':>12}")
    results = {}
    for name in arb_golden.scenarios():
        if args.only and args.only not in name:
            continue
        frames, params, expected = arb_golden.load(name)
        size = " ".join(f"{k.split('_')[0]}={len(v)}" for k, v in frames.items())
        out = arb_golden.run(name, frames, params)
        rows = len(arb_golden.expected_rows(out))
        ms = median_ms(lambda: arb_golden.run(name, frames, params), args.reps)
        results[name] = {"matcher": params["matcher"], "input": size,
                         "rows": rows, "ms": ms}
        print(f"{name:<24}{params['matcher']:<15}{size:<24}{rows:>6}{ms:>11.1f}")

    total = sum(r["ms"] for r in results.values())
    print(f"\n{'total':<24}{'':<15}{'':<24}{'':>6}{total:>11.1f}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1) + "\n")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
