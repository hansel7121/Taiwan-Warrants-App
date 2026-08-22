---
status: accepted
---

# Black-Scholes IV solved in Rust, with the Python solver kept as a fallback

Implied volatility (and the delta / leverage columns derived from it) is computed by a compiled Rust extension, `warrants_core`, built from `rust/warrants_core/`. The pure-Python/NumPy solver still exists, unmodified in behaviour, at `logic/bs_python.py`.

**Why:** tick-by-tick warrant data made the IV solve the scanner's dominant cost. The Python path is a vectorized Newton sweep with a per-row `scipy.optimize.brentq` fallback; on live data ~10% of rows fall back, and a single scalar `brentq` costs ~840 µs because `scipy.stats.norm.cdf` is expensive per call. On a 725-warrant live snapshot the whole `build_warrant_df` compute stage took **661 ms**; with the Rust engine it takes **13 ms** (≈50×), and the IV stage alone drops from ~650 ms to ~2.7 ms.

**How the numbers stay identical:** the app rounds every displayed column to 4 dp, so the contract is bit-identical output *after* `round(..., 4)`, not bit-identical roots. The Rust engine therefore mirrors the Python one exactly rather than picking a "better" algorithm: the same `[1e-6, 10]` bracket, the same certified-Newton acceptance test (tight price residual, well-conditioned scale-free vega, sigma strictly inside the bracket), the same `_refine_iv_for_rounding` pass, and a faithful port of SciPy's `brentq` C loop for the exact fallback. `norm.cdf` is `0.5 * erfc(-x/√2)` via the `libm` crate, which differs from SciPy's Cephes `ndtr` by <1 ulp — measured worst-case IV difference is 4e-13, far inside the 5e-6 rounding-edge window the refinement pass already re-solves exactly.

**Selection:** `logic/iv_engine.py` imports `warrants_core` and falls back to `logic/bs_python.py` when it is missing. `IV_ENGINE=rust` forces the extension (raising if unbuildable), `IV_ENGINE=python` forces the reference. `logic/warrant_logic.py` re-exports whichever won, so `options_logic`, `us_options_logic` and `arb_logic` are unchanged. `/healthz` reports the active engine and `wsgi.py` prints it at boot.

**Consequences:** the build now has an optional Rust step (`scripts/build_rust.sh`, wired into `render.yaml`'s `buildCommand` and a `rust-build` stage in the `Dockerfile`). It is deliberately best-effort — a host that cannot build Rust still serves correct numbers, just slowly, and the boot line says so. Parity is enforced by `tests/logic/test_iv_engine_parity.py`, which runs both engines over ~30k synthetic rows plus a full `build_warrant_df` frame and requires identical NaN placement and identical 4-dp values.

**Not done, deliberately:** `scipy.griddata` in `logic/iv_surface.py` (~10 ms for 1200 points onto the 80×80 grid) is untouched — it is now larger than the IV solve but small next to the fetch. Frontend IV (`static/js/quant.js`) is unchanged; it would need WASM, not this crate. With IV at 2.7 ms, the next real target is the Python row-parse + DataFrame assembly in `build_warrant_df`, now ~12 ms of the 15 ms compute stage.
