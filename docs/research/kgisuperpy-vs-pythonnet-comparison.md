# kgisuperpy vs. pythonnet/QuoteCom for the KGI live-price leg

Research date: 2026-08-04. Question: should the KGI leg of the live-price-streaming
feature switch from `kgisuperpy` (current integration, `origin/live-arb`) to the
friend's pythonnet + QuoteCom/TradeCom approach
(github.com/hansel7121/Taiwan-Websocket-Data), on the claim that kgisuperpy "uses
more dependencies and is therefore slower"?

No existing convention for a `docs/research/` folder was found in this repo before
this file — established here per the task.

## 1. What this repo's kgisuperpy integration actually is (verified)

- `services/broker/kgi_client.py` on `origin/live-arb` imports `kgisuperpy` directly
  and wraps `kgisuperpy.login()` / `api.Quote.subscribe_tick()` / `set_cb_tick()`.
  One connection = one `kgisuperpy.login()` call; the module holds a persistent
  session for streaming ticks. (`git show origin/live-arb:services/broker/kgi_client.py`)
- `requirements.txt` on `origin/live-arb` pins `kgisuperpy==2.1.0` with an inline
  comment: *"kgisuperpy is a pure-Python wheel but also needs KGI's native
  libCGCrypt.so installed at the system level (see Dockerfile.worker)."*
  (`git show origin/live-arb:requirements.txt`, lines 20-23)
- `Dockerfile.worker` on `origin/live-arb` documents why the worker needs a Docker
  image rather than Render's native buildpack: `libCGCrypt.so` needs `chmod 755` +
  a copy into `/lib/x86_64-linux-gnu/` (Ubuntu) or `/lib64/` (CentOS/RedHat), plus
  running KGI's own `KGI_CGCrypt genDat` cert-generation program against it. The
  actual `.so` file and the cert-generation `RUN` steps are **not yet vendored** —
  left as commented-out TODOs pending issue #22.
  (`git show origin/live-arb:Dockerfile.worker`)
- `docs/adr/0010-worker-hosting-and-command-channel.md` on `origin/live-arb`
  confirms the same: Docker (not native runtime) is required specifically because of
  `libCGCrypt.so`'s system-level install needs, and that's the whole reason the
  worker is containerized at all.

## 2. kgisuperpy's actual dependencies (verified, primary source)

Source: PyPI JSON metadata, `https://pypi.org/pypi/kgisuperpy/json` (version 2.1.0,
current as of 2026-08-04). `requires_dist`:

```
pandas, matplotlib, seaborn, requests, IPython, cryptography,
websocket-client, dash, tqdm, paramiko, numba, diskcache,
plotly==5.9.0, kaleido==0.1.0
```

- No `requires_python` or platform classifiers are declared on PyPI.
- The wheel is `kgisuperpy-2.1.0-py3-none-any.whl`, 50.4 MB
  (https://pypi.org/project/kgisuperpy/ — file listing). The `none` ABI tag on the
  wheel filename itself only means "no version-specific C-extension ABI is
  declared"; it does **not** mean the wheel is free of compiled code (see below —
  it bundles its own compiled modules regardless of what the filename tag implies).
- **Independent, more reliable evidence that it is not pure Python**: the friend's
  own `kgisuperpy_experiment/README.md` in his second repo (see §3) reports, from
  reading the *installed* package source directly:
  > "`kgisuperpy`'s own bundled compiled module (`data/_data.pyx` → `_data_url`) —
  > not fixable by picking a different version, since this is the package's own
  > code, not a swappable dependency."

  and that the quote/streaming side uses a separate native library:
  > "The quote side, though, uses a newer, separate native library called
  > 'StarWave' (`marketdata/starwave/`), accessed via `ctypes`, not pythonnet."

  So: kgisuperpy bundles its own compiled (Cython) extension and a native
  `ctypes`-loaded shared library ("StarWave") for quotes, independent of
  `libCGCrypt.so`. It is not the "pure Python" package the wheel filename suggests.

- `pandas`, `numba`, and their transitive deps (`numpy`, LLVM-based `llvmlite` for
  numba's JIT) are themselves compiled/native packages, not pure Python — so the
  "more dependencies" framing is true in a literal package-count sense, but most of
  that list (`matplotlib`, `seaborn`, `dash`, `plotly`, `kaleido`, `IPython`,
  `tqdm`, `diskcache`, `paramiko`) is charting/notebook/backtest tooling, not
  anything on the hot path of receiving and dispatching a tick callback.

## 3. What github.com/hansel7121/Taiwan-Websocket-Data actually uses (verified, primary source)

Fetched live via `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/...` on
2026-08-04.

- `requirements.txt` (root):
  ```
  pythonnet
  matplotlib
  python-dotenv
  ```
- `kgi_config.py` loads credentials from env vars (`KGI_TOKEN`, `KGI_SID`,
  `KGI_USER_ID`, `KGI_PASSWORD`, `KGI_BROKER_ID`, `KGI_ACCOUNT`) via
  `python-dotenv` — these are QuoteCom/TradeCom-style credentials, not
  kgisuperpy's `person_id`/`person_pwd`.
- `README.md` confirms: *"Python scripts for live Taiwan market data (stocks,
  warrants, TAIFEX options) via KGI's QuoteCom API, plus order execution via
  TradeCom."*
- `QuoteComExamplePy/` and `TradeComExamplePy/` each vendor KGI's **.NET DLLs**
  directly: `Package.dll`, `PushClient.dll`, `QuoteCom.dll` / `TradeCom.dll`,
  `Interop.KGICGCAPIATLLib.dll`, `ICSharpCode.SharpZipLib.dll`, loaded via
  `clr.AddReference(...)` (confirmed in `to_test/test_cmoney_vs_kgi.py` and
  `to_test/test_subscription_concurrency.py`, both of which do
  `import clr; clr.AddReference("Package")` etc. and
  `from Intelligence import QuoteCom, COM_STATUS, DT`).

  **This confirms the claim about mechanism is correct**: the friend's repo really
  does use `pythonnet` (the `clr` module) to load KGI's legacy QuoteCom/TradeCom
  .NET DLLs, not `kgisuperpy`.

- **Important finding not in the friend's claim**: this same repo also contains a
  `kgisuperpy_experiment/` folder — the friend himself tried `kgisuperpy` directly
  and documented the results in its `README.md`. Key excerpts:
  - `kgisuperpy 2.1.0` "pulls in `pandas`, `numba`, and its own bundled compiled
    (Cython) modules" — corroborates §2 above from the friend's own installed-source
    inspection, independent of PyPI metadata.
  - The experiment was **blocked entirely by Windows Smart App Control** rejecting
    unsigned/unrecognized native DLLs/PYDs three separate times (pandas's Cython
    extensions, numba's compiled extension, and kgisuperpy's own bundled
    `_data.pyx`/`_data_url` module) — investigation stopped there. **No successful
    login, no live session, and no timing/speed measurement of any kind was ever
    taken.**
  - **Correction (2026-08-05, supersedes the claim below)**: the `TXF*`-only
    whitelist in `marketdata/starwave/sw_subscription_rule.py` is scoped to the
    **`SWQuote`/StarWave channel** (`Quote_sw.py`) only — a secondary,
    stock-quote-oriented channel where `TXF*` futures are handled as a fallback
    (`if symbol.startswith('TXF')`, `Quote_sw.py:94`). That is not the channel
    this app should use for TAIFEX options. `kgisuperpy` exposes a separate,
    dedicated **`FutQuote` channel** (`api.FutQuote`, backed by `_FutQuote` in
    `Quote.py:318` / `PRoboQuoteAPI` in `marketdata/quote.py`, not StarWave)
    covering TW futures *and* options together — mirroring how TAIFEX itself
    groups the two product families. Confirmed directly from the vendored
    source:
    - `_FutQuote.subscribe_tick` / `subscribe_bidask` / `subscribe_all`
      (`Quote.py:318-384`) — same push-subscription shape as the stock `Quote`
      channel; its symbol check (`_symbol not in self._list`) validates against
      `self._api.Contracts.keys()`, a live/dynamic contract list, not a
      hardcoded whitelist.
    - `trading/FutOrder.py`'s own docstring gives `"TXO23100L4"` as a worked
      `symbol` example tagged `（選擇權）` = option (`FutOrder.py:104`),
      confirming TXO (TAIFEX index options) is a first-class symbol here, not
      just futures.
    - `marketdata/contract.py:65` defines `class Option(Contract)` with
      `contract_type = ContractType.Option`, `strike_price`, `call_put`,
      `contract_month` — a full option schema, separate from `Future`.

    So: **kgisuperpy does carry TAIFEX options (TXO) websocket data** — via
    `api.FutQuote`, not the stock `Quote` channel and not `SWQuote`. The
    original claim below (kgisuperpy "cannot provide TAIFEX options data at
    all") was wrong; it conflated the `SWQuote` fallback channel's whitelist
    with kgisuperpy's overall capability. kgisuperpy actually has three quote
    channels — `Quote` (TW stocks/warrants), `USQuote`, `FutQuote` (TW
    futures+options) — and the whitelist only ever applied to the first.
    `kgi_client.py` today only wires up `Quote`; if TW-option live data becomes
    in-scope (the deferred #19 epic), `FutQuote` is the entry point — no SDK
    swap or workaround needed.

  - Original (superseded) claim, kept for the record: "kgisuperpy's quote layer
    (`marketdata/starwave/sw_subscription_rule.py`) has a hardcoded client-side
    whitelist that excludes TAIFEX options entirely (`TAIFEX: {"whitelist":
    ["TXF*"]}` — matches index futures only, not `CDO*` stock options or `TXO*`
    index options). If this app ever wants KGI-sourced TW *options* data (today
    it's warrants only), kgisuperpy cannot provide it at all, regardless of
    speed." — see correction above.

- No issues or pull requests exist on `hansel7121/Taiwan-Websocket-Data`
  (`gh api repos/hansel7121/Taiwan-Websocket-Data/issues` and `/pulls` both return
  empty) — so there is no discussion thread there to check for benchmark data
  either.

## 4. What pythonnet concretely requires (verified, primary source)

Source: `https://github.com/pythonnet/pythonnet` (README, fetched 2026-08-04).

- **Runtime dependency**: *"By default, Mono will be used on Linux and macOS, .NET
  Framework on Windows."* CoreCLR is available as an opt-in
  (`PYTHONNET_RUNTIME=coreclr` or `pythonnet.load("coreclr")`), "provided .NET Core
  is installed in a default location or the `dotnet` CLI tool is on the PATH."
- So on Linux (where this app's Docker worker runs), pythonnet needs a **Mono
  runtime installed at the system/OS level** — this is a new OS-level dependency
  class entirely absent from the kgisuperpy path (which needs one `.so` file, not
  a full CLR runtime).
- pythonnet's own README/docs, as fetched, contain **no documented performance
  benchmarks or overhead figures** for the Python↔.NET bridge itself.
- Independent evidence *against* the "fewer deps ⇒ faster" framing: a real,
  primary-source pythonnet GitHub issue (`pythonnet/pythonnet#694`,
  "Performance: pythonnet more than 400x slower than identical C#") documents that
  **repeated small calls across the Python↔.NET boundary carry meaningful
  marshaling overhead** — 0.7865 ms for equivalent C# vs. 388.7 ms for the
  pythonnet-mediated call over 100,000 iterations in that reporter's benchmark.
  This is the opposite direction of evidence from what the friend's claim would
  predict: if anything, a pythonnet-mediated tick callback path has a documented
  per-call interop tax that a pure-Python (or ctypes-based) path does not.
  (This is one user's microbenchmark on a narrow case, not a general verdict on
  pythonnet — cited here only to show that "pythonnet is inherently the fast
  option" is not established either.)

## 5. Evidence for the speed claim itself

**None found.** Specifically checked and came up empty:
- `hansel7121/Taiwan-Websocket-Data` issues and PRs — both empty.
- GitHub code/issue search for `kgisuperpy speed`, `kgisuperpy slow` — no results.
- Web search for `"kgisuperpy" pythonnet OR QuoteCom benchmark speed comparison` —
  no results; general Python benchmarking sites only, nothing KGI-specific.
- KGI's own SuperPy docs (`superpy.kgieworld.com.tw/kgipythonapi/faq`, referenced
  secondhand via the friend's own experiment README) were not found to contain any
  performance claims either — the friend's README only cites them for connection
  tier limits (新星/菁英/尊爵), not speed.
- The friend's own `kgisuperpy_experiment/` — the one place in either repo where he
  actually tried to run kgisuperpy — never got past a login/import blocker
  (Windows Smart App Control), so **he never measured kgisuperpy's runtime speed
  himself**. His README documents this explicitly and stops at "investigation
  stopped there."

**The claim conflates two different things.** "More dependencies" (verified true —
14 declared PyPI deps vs. pythonnet's near-zero-dep footprint) is a *package-count*
observation. "Therefore slower" is a *runtime-performance* claim. Nothing in either
repo, PyPI, or a web/GitHub search connects the two for this specific pair of SDKs.
A large `requires_dist` list dominated by charting/notebook/backtest packages
(`matplotlib`, `seaborn`, `dash`, `plotly`, `IPython`) does not, by itself, imply
slower per-tick callback latency — most of that surface never executes on the
streaming hot path this app actually uses (`login` → `Quote.subscribe_tick` →
`set_cb_tick` callback). The friend's own claim, as relayed, gives no mechanism
(no profiling, no callback-latency measurement) connecting "many pip packages" to
"slow tick delivery."

## 6. Practical deployment cost of switching (assessment)

Today (kgisuperpy, per `origin/live-arb`):
- Docker is already required, solely for `libCGCrypt.so` (a single `.so` file:
  `chmod 755` + copy to a lib dir + one cert-gen invocation). This cost is already
  paid regardless of which SDK is chosen, since `kgisuperpy` needs it.

If switching to pythonnet + QuoteCom/TradeCom:
- Add a Mono (or CoreCLR) runtime install to `Dockerfile.worker`. A full
  `mono-complete` layer is commonly cited in the ~240-260 MB compressed range on
  Docker Hub (e.g. the official `mono` image and `0xff/mono-complete`, both in that
  band per Docker Hub's published layer sizes) — this is on top of, not instead of,
  the existing `libCGCrypt.so` step, since the friend's DLLs
  (`Interop.KGICGCAPIATLLib.dll`, `PushClient.dll`, etc.) are the .NET-side
  equivalents wrapping KGI's C API and are a separate artifact from `libCGCrypt.so`
  handling. *(Sizes here are general Docker Hub figures for Mono images, not a
  measurement of this specific worker image — treat as an order-of-magnitude
  estimate, not a verified exact number for this repo.)*
- Trade a `pip install`-able, PyPI-hosted dependency (kgisuperpy) for one requiring
  vendored `.dll`/`.doc`/`.docx` files checked into (or fetched into) the repo and a
  `clr.AddReference()` load path pointing at a filesystem directory — a less
  standard, harder-to-pin deployment shape than a pinned PyPI wheel.
- Inherit a new failure surface: Mono/CoreCLR runtime version drift, `clr` module
  compatibility, and cross-platform (Linux server vs. the friend's own Windows dev
  machine, per his README) behavior differences that were never exercised on Linux
  in his repo — everything in `to_test/` was written and run against a Windows path
  (`assembly_path = r"C:\Users\user\..."`), not validated on the Linux/Docker target
  this app would actually deploy to.

**Bottom-line assessment: switching is not justified by the evidence gathered.**
The friend's claim ("more dependencies ⇒ slower") has no supporting measurement
anywhere — not in his repo, not in kgisuperpy's docs, not in a web search, and his
own attempt to test kgisuperpy directly never got far enough to produce a number.
Meanwhile the switch has real, verifiable costs: a new system-level Mono/CoreCLR
runtime dependency (on top of, not replacing, the existing `libCGCrypt.so` step
this app already carries), a less standard vendored-DLL deployment shape instead of
a pinned PyPI package, and a Windows-developed codepath with no evidence of Linux
validation. The one concrete functional difference that *does* favor
QuoteCom/TradeCom, found in the friend's own experiment notes, is that kgisuperpy's
StarWave quote layer hard-blocks TAIFEX options subscriptions client-side — but
that's a feature-coverage question (relevant only if this app later wants
KGI-sourced TW options data, not warrants), completely separate from the speed
claim under review, and this app's current KGI use is warrants-only.

## Sources

- `git show origin/live-arb:services/broker/kgi_client.py`
- `git show origin/live-arb:requirements.txt`
- `git show origin/live-arb:Dockerfile.worker`
- `git show origin/live-arb:docs/adr/0010-worker-hosting-and-command-channel.md`
- https://pypi.org/project/kgisuperpy/
- https://pypi.org/pypi/kgisuperpy/json
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/requirements.txt`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/kgi_config.py`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/README.md`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/kgisuperpy_experiment/README.md`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/to_test/test_cmoney_vs_kgi.py`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/to_test/test_subscription_concurrency.py`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/QuoteComExamplePy`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/contents/TradeComExamplePy`
- `gh api repos/hansel7121/Taiwan-Websocket-Data/issues` and `/pulls` (both empty)
- https://github.com/pythonnet/pythonnet (README)
- https://github.com/pythonnet/pythonnet/issues/694
- Docker Hub `mono:latest` / `0xff/mono-complete` layer size listings (general Mono
  image size reference, not this repo's own measurement)
