---
status: accepted
---

# Rust past the IV solver: matchers, the warrant frame, and where it stops

Extends ADR-0003 rather than superseding it. That one moved the Black-Scholes IV/delta kernels into `rust/warrants_core`; this one covers everything measured after, and — as importantly — what was deliberately left in Python and why.

**Why:** tick-by-tick data made per-tick compute the binding constraint, and ADR-0003 moved the bottleneck rather than removing it. Profiling the rest of the app reordered the obvious priorities. The arb matchers were 100–1000× larger than every frame builder combined (`_match_warrants_pcp` at 21 s on W=200 O=300 in the first survey), while response encoding — two of whose three serializations were pure waste — was bigger than the entire remaining warrant compute stage.

## What the profiler actually said

Two premises the work started from turned out to be wrong, and both changed the plan:

- **The arb matchers were not IV-bound.** A survey run without the extension built put 64–87 % of their time in scalar `implied_vol`. With it built, batching those solves was ~5 % *slower* — the added frame copies outweighed it. cProfile on a 338×59 chain put ~90 % of `_match_warrants_to_options` in pandas (`Series.__init__`, `DataFrame.__setitem__`, `iterrows`) and none of it in the solve. The matcher rebuilt a filtered DataFrame copy per warrant and assigned four derived columns to it: 2·W frame copies and 8·W column inserts per scan.
- **numpy was not automatically the answer either.** Per-warrant masks over ~30-element candidate slices cost more in call overhead than a plain Python loop at that size. Extracting `_match_warrants_to_options`' scan into a plain-Python kernel took it from 33.5 ms to 17.6 ms *before* any Rust.

The lesson worth keeping: measure with the extension built and at production input sizes. A survey against the pure-Python engine points at the wrong thing.

## The FFI contract

**Rust returns column arrays and row indices. Never strings, never nested dicts, never pandas, never a finished JSON buffer.**

The deciding constraint is the Supabase snapshot read path (`options_logic.read_tw_option`, and the warrant equivalent), which builds the *same frames* from Postgres and feeds the *same* filters without ever calling `build_warrant_df`. A richer return type would force a second, divergent implementation of everything downstream. Column arrays let `pd.DataFrame(cols, copy=False)` wrap them as blocks and leave every consumer — CSV routes, arb, the scheduler, the snapshot path — untouched.

The matcher kernels return row indices plus computed numerics; Python assembles the ~35-key output rows by index. That keeps the field names, the `None`-vs-`NaN` conventions and the rounding in one diffable place, and means Rust never has to know what `pd.notna` means.

**Standing rule:** nothing Rust-owned ever enters a TTL cache, Supabase, or a request boundary. Caches hold Python-native payloads or plain numpy/pandas, so a cache hit is engine-independent.

## Rounding is a correctness surface

`arb_logic` and `warrant_logic` round with Python's builtin `round(x, n)` — correctly-rounded *decimal* rounding, ties to even — while `_refine_iv_for_rounding` uses NumPy's scale-rint-unscale. The two disagree on ties. Rust reproduces the builtin by formatting and re-parsing (`arb::round_py`), verified against CPython over 600k values plus the usual tie cases.

This is not theoretical. Passing option columns to the direct kernel as numpy arrays instead of lists made `round(np.float64, 2)` fire instead of `round(float, 2)`, and surfaced as a recorded `price_diff_pct` of −42.85 coming back as −42.86.

## The engine switch

`RUST_ENGINE=rust\|python\|auto` (default `auto`), read once at import, binding module-level names — never a per-call `if RUST:`. `rust` is a hard requirement and raises; `auto` falls back silently, which is why the engine is reported per feature on `/healthz` and at boot. `RUST_ENGINE_OFF=arb,warrant_frame` disables named features individually, so one misbehaving kernel can be dropped back in production without reverting everything. `IV_ENGINE` still works as the old name.

Every ported function has an **identical signature in both engines**, taking and returning plain numpy/list/dict. That constraint is what makes each independently swappable, testable and benchmarkable.

## The safety net came first

`arb_logic` had five matchers, three orchestrators, ~1400 lines, and exactly one test touching any of it — the `NoMatchesError`-vs-`RuntimeError` contract on empty frames. Nothing could be changed with confidence.

Twenty scenarios were recorded from live chains before any change (`scripts/capture_arb_fixtures.py`, `tests/fixtures/arb/`), covering both `positive_loose` directions, warrants with no live bid, ask-only and bid-only books, `exercise_ratio <= 0`, solved-IV vs NaN-IV input frames, both PCP synthetic modes, butterfly, the TW/US leg matcher, and (until the tab was removed) straddle end to end including its stage-diagnosis string. Frames are stored as JSON with an explicit dtype map, not CSV or Parquet: the option frames carry `None` in object-dtype columns while the warrant frames carry `NaN` in float columns, `to_json` renders both as `null`, and a change that swapped one for the other would pass every value-level check while breaking `.fillna`, `> 0` comparisons and the Supabase writer.

Regenerating a fixture to make a test pass is the one action that destroys the net; `tests/fixtures/arb/README.md` says so.

## Not done, deliberately

- **`scipy.griddata`** (`logic/iv_surface.py:19`). Warrant strikes are round numbers and DTEs are integers, so the point cloud lies on a **regular lattice** — cocircular quads are the normal case, not a degenerate one. Qhull's diagonal choice on a lattice quad is its own tie-break; any Rust Delaunay flips a substantial fraction, moving interior interpolated values. It would fail the parity bar on real data, for 9.8 ms on a route opened rarely. If it ever needs to be faster, reuse one `scipy.spatial.Delaunay` across both routes, or memoise — stay inside Qhull.
- **The static-arb LP** (`logic/static_arb.py`). `linprog(method="highs")` returns *a* vertex of the optimal face; these LPs are structurally degenerate, so the optimal value is unique but `res.x` is not. A different vertex flows through `_repair_integers` into a different `legs` list, which is the Static Arb tab's entire visible output. Not a tolerance problem — the answer is legitimately non-unique.
- **WASM in the browser.** The only per-drag numeric path was `dashboard.js::renderPayoff` at ~600 Black-Scholes evaluations per slider tick — sub-millisecond in V8. The cost was `Plotly.newPlot` rebuilding the plot on every `oninput`. Fixed with `Plotly.react` plus a `requestAnimationFrame` coalescer, which is the actual bug.
- **The TW option MIS frame.** Profiling `read_tw_option` for one underlying showed it entirely network-bound — SSL reads and curl at the top, compute absent. The ~10k-quote parse the plan worried about is the scheduler's whole-universe fetch, not a per-request chain, and that path is the trickiest to port (regex decode, `pd.Timestamp` arithmetic, weekly labels, and the `None`-vs-`NaN` object-dtype trap). Left for when a live profile justifies it.
- **`_match_warrants_pcp`'s scan in Rust.** After the pandas work it is 17 ms on W=338 O=59, down 29× from 485 ms. The kernel pattern is proven by the butterfly and direct ports; PCP is no longer where the time is.
- **`sort_values` ladders** (pandas' default sort is not stable; reimplementing changes tie-break order for microseconds), **`adr_premium_scenario`** (already vectorized NumPy over ~750 points), and **`_adf_test`** (reimplementing statsmodels' lag selection and MacKinnon tables is a guaranteed parity failure for a once-per-modal-open call).

## Consequence

`scripts/bench_engines.py` runs every ported path against the committed fixtures under either engine and prints input sizes alongside timings — everything in `arb_logic` is O(W×O), so a bare "8× faster" means nothing without them. Numbers are reported single-threaded: `CLAUDE.md` pins the deploy to a 1-vCPU Render standard instance, where rayon buys nothing.
