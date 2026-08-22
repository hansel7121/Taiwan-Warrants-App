# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow

- **Never commit or push directly to `main`.** Two people work on this repo; direct pushes to `main` from multiple people is what causes conflicts.
- Before starting any new work, pull `main` first (`git pull origin main`) so you're branching from the latest code. A `SessionStart` hook already runs this automatically at the start of every session.
- Create a feature branch off `main` for the change (e.g. `yourname/short-description`), commit and push to that branch as you go — don't batch many changes into an unpushed pile.
- Open a PR (`gh pr create`) instead of pushing to `main` directly. Before merging, pull `main` into the branch again and resolve any conflicts, since a teammate may have pushed to `main` directly in the meantime.
- **Commit message format:** `type: short summary` (`feat`, `fix`, `refactor`, `docs`, `chore`). No trailing period on the summary line. Use the body to explain *why*, not what — the diff already shows what changed.
- **Do NOT add Claude / the assistant as a contributor.** Never put a `Co-Authored-By: Claude ...` (or any `Co-Authored-By` / author trailer pointing at the assistant or `noreply@anthropic.com`) in commit messages. Commits must be authored by the human only. This overrides any default that would append such a trailer.
- Code edits happen directly in the main chat. Do NOT spawn a subagent to implement code unless the user explicitly asks for one — a fresh subagent starts with no context and has to re-derive everything, which costs more than it saves for most changes.

## Comments

- Top-of-file comment: state what the file is for, and how its important functions/classes are used in the bigger picture. Nothing more.
- Per function/class: one line only, brief. No paragraphs.

## What this app does

A local Flask web app for scanning Taiwan stock warrants and equity options, computing implied volatility, and surfacing arbitrage between warrants and TAIFEX/US options. It runs as a server that auto-opens in the browser and serves a single-page frontend with four tabs:

1. **Warrant Scanner** — live warrant data from CMoney; computes IV (Black-Scholes via Brent's method on the ask), delta, real leverage, time value / time value %. Filter by type, DTE, min leverage, max time-value %, min volume.
2. **IV Surface** — Plotly 3D surface of warrant IV across strike and DTE (80×80 grid interpolated with `scipy.griddata`), plus scatter of the raw points.
3. **Options Scanner** — live TAIFEX equity option data (and US ADR options); IV/delta/leverage per contract; flags whether each quote is a live bid/ask or a settlement-price fallback (`is_live`).
4. **Arb Finder** — cross-market mispricing between warrants and options. Modes: **Direct Match** (same-type call/put monotonicity: buy warrant / sell option), **PCP Match** (put-call parity via synthetic replication), **Straddle Vol Arb** (long cheapest-IV package vs short dearest-IV option package), plus **Warrant vs US Option** and **TW Option vs US Option** cross-market matches. Clicking a row opens a trade-breakdown modal with per-leg cash flows and a P&L chart (intrinsic-at-expiry + mark-to-market at a slider DTE).

There is also a **Portfolio** tab (persisted per-user to Supabase) with a **Suggestions** sub-tab fed by an automated scanner (see below).

Supported underlyings and their option IDs are managed as data in Supabase product tables (`warrant_stocks`, `tw_option_products`, `us_option_products`) — e.g. 2330 (TSMC), 2303 (UMC), 2603, 2881, 2882, plus TXO. Add/remove via the product routes; nothing is hard-coded.

## Commands

Requires Python 3.11+. Conda env: **`warrants`**. The Rust engine additionally needs a stable Rust toolchain (`scripts/build_rust.sh` installs one via rustup if absent).

```bash
conda activate warrants
pip install -r requirements.txt

# Optional but wanted: compile the Rust engine (installs rustup if missing).
# Without it the app runs on the Python fallbacks — same numbers, slower.
./scripts/build_rust.sh

# Benchmark every ported path against the committed fixtures, either engine
python scripts/bench_engines.py
python scripts/bench_iv.py

# Dev server — ALWAYS prefix with the Taiwan timezone (see note)
TZ=Asia/Taipei python app.py                 # http://127.0.0.1:5001

# Production-style (what deployment runs)
TZ=Asia/Taipei gunicorn -w 1 --threads 8 --timeout 240 -b 0.0.0.0:5001 wsgi:app
```

Stop the dev server with `Ctrl+C` (or `pkill -f "python app.py"`).

Run the suite under **both** engines before pushing — the Python fallbacks are
only exercised when you ask for them:

```bash
TZ=Asia/Taipei python -m pytest tests -q                    # as it ships
RUST_ENGINE=python TZ=Asia/Taipei python -m pytest tests -q # every fallback
```

- **Always set `TZ=Asia/Taipei`.** Days-to-expiry is computed against the machine's local "today". West of Taiwan (e.g. US Pacific), the local date lags Taiwan for part of the day, so an already-expired batch leaks into results and counts inflate (~1580 locally vs ~1407 on the UTC deploy). The `TZ` prefix scopes Taiwan time to this process only.
- **Exactly 1 gunicorn worker.** Market-data caches live in process memory and would diverge across workers; the APScheduler jobs share those caches too. Use threads for concurrency, never extra workers.
- **No-login local mode.** With `APP_ENV=local` and `LOCAL_USER_ID` set in `.env`, the app runs with no login and acts as that fixed user, syncing its portfolio with the shared Supabase (`services/auth.py::local_mode`). `.env` lives at the repo root (loaded by `services/db.py`). See `SETUP-LOCAL.md`.
- **macOS:** if gunicorn workers die with `objc ... fork()` errors on yfinance routes, prefix with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`. Linux is unaffected.

## Architecture

Three layers: **routes** (`app.py`), **pure computation** (`logic/`), **infrastructure** (`services/`). Keep them separated — `logic/` touches no Flask context and no Supabase; `app.py` does only request/response work.

```
app.py                 Flask routes only (parse request -> call logic/services -> JSON/CSV)
wsgi.py                gunicorn entry point (wsgi:app); starts the scheduler in production
logic/                 pure market-data + math, no side effects, own in-process caches
  iv_engine.py           engine registry: binds each feature to Rust `warrants_core` or its Python twin
  bs_python.py           Python Black-Scholes reference (price, delta, vega, IV) — fallback for `iv`
  arb_kernels_py.py      Python arb-matcher kernels — fallback for `arb`
  warrant_frame_py.py    Python warrant-frame builder + COL_ORDER — fallback for `warrant_frame`
  warrant_logic.py       CMoney warrant fetch; re-exports the frame builder + IV kernels; cmkey + universe scrape
  options_logic.py       TAIFEX TW option fetch + computation; R (risk-free rate); _commodity_map()
  us_options_logic.py    US ADR option fetch, TWD conversion; R_US; _adr_map(); contract_tw_shares()
  arb_logic.py           warrant<->option matching, put-call parity, straddle vol-arb, TW/US leg arb
services/              side-effecting infrastructure
  db.py                  Supabase client (loads root .env; service-role key)
  store.py               per-user portfolio persistence (Supabase + local JSON mirror, tombstone sync)
  auth.py                Supabase magic-link JWT verification + email allowlist; require_auth; local_mode
  scheduler.py           APScheduler background refresh + suggest jobs (single-worker, market-hour gated)
  db_market.py           market-data snapshot read/write (batch-pointer model)
  db_products.py         tracked-product CRUD (warrant/TW-option/US-option lists)
  db_suggestions.py      arb_suggestions CRUD (automated Direct-Arb output)
  applog.py memlog.py    request logging + memory/timing measurement
templates/index.html   single-page frontend shell (Jinja + Plotly)
static/css/app.css     extracted stylesheet
static/js/*.js         extracted JS (common, quant, scanners, arb, portfolio)
rust/warrants_core/    Rust extension: src/bs.rs (IV/delta), arb.rs (matchers), frame.rs (warrant frame)
tests/fixtures/arb/    recorded matcher output — pins current behaviour; see its README before touching
supabase/              schema.sql (current end state) + migrations/ (incremental)
notebooks/             exploratory research (ADR parity, screening); commit WITHOUT outputs
scripts/               one-off maintenance/seeding scripts
```

**Data sources:**
- **Warrants** — CMoney private API (`mainpage.ashx`); requires a session `cmkey` token scraped from the warrantsquery page HTML over plain HTTP (no browser — there is no Playwright/Chromium here). Refreshed on error `-3` (expired) and periodically by the scheduler.
- **TW options** — TAIFEX public CSV download (`optDataDown`) + TAIFEX MIS for live intraday quotes.
- **US ADR options** — yfinance, pre-converted to TWD-per-Taiwan-share by `us_options_logic`.
- **Spot prices** — TWSE MIS API, Yahoo Finance / yfinance fallbacks.

**Key computations (`logic/`):**
- IV solved with Brent's method (`warrant_logic.implied_vol`), bounds `[1e-6, 10.0]`.
- **Three features run in Rust** (`rust/warrants_core`): the IV/delta kernels (`iv`), the arb matcher kernels (`arb`), and the warrant-frame builder (`warrant_frame`). `logic/iv_engine.py` is the registry — it binds each to the extension or its Python twin **once at import**, never per call. `RUST_ENGINE=rust|python|auto` picks the backend (`IV_ENGINE` still works as the old name); `RUST_ENGINE_OFF=arb,warrant_frame` disables named features individually; `/healthz` and the boot log report the engine **per feature**.
- Both engines must produce identical output after `round(..., 4)` — same values, same NaN/None placement, same row order, **same dtypes**. Enforced by `tests/logic/test_iv_engine_parity.py`, `test_arb_engine_parity.py` and the recorded fixtures in `tests/fixtures/arb/`. **Any change to one implementation must be mirrored in the other**, or those tests fail.
- Rust returns **column arrays and row indices** — never strings, nested dicts, pandas or finished JSON. The Supabase snapshot path builds the same frames from Postgres and feeds the same filters, so a richer return type would need a second implementation of everything downstream. Nothing Rust-owned ever enters a TTL cache, Supabase or a response.
- ⚠️ `arb_logic`/`warrant_logic` round with Python's builtin `round(x, n)` (decimal, ties to even) while `_refine_iv_for_rounding` uses NumPy's `np.round` (scale-rint-unscale). **They disagree on ties.** Passing a numpy array where a list is expected silently switches which one runs. Rust reproduces the builtin via `arb::round_py`.
- See `docs/adr/0004-rust-engine-expansion.md` for what was deliberately NOT ported (`griddata`, the static-arb LP, WASM, the MIS frame) and why.
- Black-Scholes delta with a continuous risk-free rate: Taiwan CBC benchmark `options_logic.R` (~1.875%); US leg uses `us_options_logic.R_US`.
- All TW single-stock options: exercise ratio = **2,000 shares/contract**; TXO index = **50 NT$/point**.
- Warrant per-underlying-share price = `warrant_ask / exercise_ratio`, and units-to-cover = `contract_size / exercise_ratio` — the normalization used across every arb path (`logic/arb_logic.py`, `_match_warrants_to_options` ~L399/L407).

**Routes worth knowing** (all in `app.py`, all `require_auth` except `/`, `/login`, `/healthz`):
- Data: `/read_warrant`, `/read_tw_option`, `/read_us_option` (+ `_csv` variants), `/iv_surface`, `/iv_surface_options`, `/adr_premium[_scenario]`.
- Arb: `/match_warrant_tw_option`, `/match_warrant_us_option`, `/match_tw_us_option`, `/straddle_arbitrage` (+ `_csv`).
- Portfolio: `/get_portfolio`, `/save_portfolio`, `/close_quote`.
- Suggestions: `/list_suggestions`, `/remove_suggestion` (hard delete).
- Products: `/list|add|remove_warrant_stock`, `/lookup_warrant_stock`, `/list|add|remove_tw_option_product`, `/list|add|remove_us_option_product`.
- Manual refresh: `/sync_warrant`, `/sync_tw_option`, `/sync_us_option`, `/sync_universe` (debounced, run the scheduler's core writers synchronously).

**Scheduler (`services/scheduler.py`):** one `BackgroundScheduler` with a single-worker executor, started once from `wsgi.py` (prod) or `app.py __main__` (dev). Jobs: `cmkey` (interval, ungated), `universe` (daily 07:00 TPE cron), and three intraday data syncs (`warrants`/`tw_options`/`us_options`) on a wall-clock 15-min grid, each `_gated` to its market's hours. The **suggest job** (`sync_suggestions`) runs a few minutes after the grid, gated on `tw_equity` hours: it scans the Direct tab's two strategies (`same_type`, `pcp`) via `arb_logic.match_warrant_tw_option` over the warrant∩tw-option universe, drops non-executable (short-warrant) PCP rows, and upserts profitable rows into `arb_suggestions` (stale rows flipped, not deleted). The Portfolio → Suggestions sub-tab reads them via `/list_suggestions`.

**Supabase schema (`supabase/schema.sql`, migrations in `supabase/migrations/`):**
- `allowed_users` — email allowlist for auth.
- `portfolio` — per-user positions, RLS "own rows"; `deleted_at` tombstone + `updated_at` sync cursor for two-way Render/local sync.
- `md_*` + `md_batches` + `cmoney_key` — server-only market-data snapshots (service-role key, RLS enabled with **no policy** — never add one). Batch-pointer model: write a new batch, flip `md_batches`, delete the old batch (supabase-py has no transactions, so the pointer flip is the atomicity mechanism).
- `warrant_stocks` / `tw_option_products` / `us_option_products` — shared tracked-product lists (server-only).
- `arb_suggestions` — automated Direct-Arb output; deterministic `id` = `{arb_type}:{warrant_code}:{option_contract}` so re-finding upserts the same row; server-only.

**Notebooks:** `notebooks/` holds exploratory research. Commit **without outputs** (`jupyter nbconvert --clear-output --inplace notebooks/*.ipynb`) to keep diffs small.

## Deploy (Render)

The build runs `pip install -r requirements.txt && ./scripts/build_rust.sh`; the Rust step is best-effort, so a build image without a toolchain still deploys (on the Python IV solver). The `Dockerfile` compiles the same crate in a separate `rust-build` stage. Check `/healthz`'s `iv_engine` field after a deploy to confirm which engine is live.

The repo ships a `render.yaml` blueprint — create a Render **Blueprint** from the repo; it provisions a native Python web service running gunicorn with a single worker on the **standard** plan (1 vCPU / 2 GB — the starter tier's 512 MB proved too tight for pandas/numpy/scipy, see issue #57). Set these secrets in the dashboard (marked `sync: false`, never committed): `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`. Post-deploy, add the live Render URL to Supabase **Auth → URL Configuration → Redirect URLs** or magic-link sign-in fails.

## Taiwan warrant & option mechanics

These are the domain rules that govern all pricing, sizing, and P&L math in this app. Get these wrong and every arb/hedge number is off.

### Units and board lots

- A warrant's smallest tradable *unit* is 1 warrant, but warrants **trade only in board lots (張) of 1,000 units**. Any quantity that must actually be executed has to be a whole number of board lots.
- Convert units to board lots (張): **divide units by 1,000**. A fractional 張 is a theoretical/exact hedge size; a real order rounds to a whole 張, leaving residual delta.

### Warrant pricing

- Warrants are **priced per unit**. A quote of `P` NTD means one *unit* costs `P` NTD.
- Therefore one **board lot costs `P × 1,000` NTD**.

### Exercise ratio (warrants)

- The **exercise ratio is the number of underlying shares delivered per single warrant *unit*** (not per board lot). Often fractional (e.g. 0.5 shares/unit).
- Consequences:
  - One board lot (1,000 units) delivers `1,000 × exercise_ratio` shares.
  - **Price per underlying share** = `warrant_unit_price / exercise_ratio` — how the app normalizes warrant prices for cross-instrument comparison (`logic/arb_logic.py::_match_warrants_to_options`, `warrant_ask_per_share = ask / ratio`).
  - **Units needed to cover `N` shares** = `N / exercise_ratio`; board lots = `(N / exercise_ratio) / 1,000` (same file, `warrants_needed = round(opt_contract_size / ratio)`).
- ⚠️ Common mistake: exercise ratio is **per unit, not per board lot**. A board lot delivers `1,000 × exercise_ratio` shares, not `exercise_ratio`.

### Warrant style, settlement & the short constraint

- Taiwan warrants are **cash-settled** — exercise/expiry pays cash against the settlement price; the holder receives no shares.
- Warrants are **American-exercisable** but in practice traded (sold), not exercised — priced like tradable American-style cash-settled contracts.
- ⚠️ **Warrants are LONG-ONLY. You cannot write or short a warrant.** A retail/desk participant can only *buy* warrants and later sell to close. Any arb/hedge structure requiring a **short warrant leg is not executable** — only warrant-held-long strategies are valid. This is a hard constraint on every warrant-side arb (the code marks such rows `executable=False` / debug-only).

### Equity options (for comparison)

- Taiwan single-stock equity options always have an **exercise ratio / contract size of 2,000 shares** per contract.
- Options are **priced per point (per share), not per unit**: a quote of `2` NTD is worth `2 × 2,000 = 4,000` NTD per contract. The arb code multiplies the per-share price by `opt_contract_size` (2,000).
- Taiwan equity options are **European style** — exercisable only at expiry.
- Taiwan equity options are **cash-settled** — no physical delivery.
- Because TW options are European + cash-settled, a **short TW option leg carries no early-assignment risk**. US ADR options are **American** (early-exercisable): a short US leg *does* carry early-assignment risk. This asymmetry matters for every TW/US comparison.

### Quick reference

| Instrument | Priced per | Multiplier to get one lot/contract cost | "Ratio" meaning | Style | Settlement | Short allowed? |
|---|---|---|---|---|---|---|
| Warrant | unit | × 1,000 (units per 張) | shares delivered **per unit** | American-exercisable (traded, not exercised) | Cash | ❌ Long-only — cannot write/short |
| TW equity option | point / share | × 2,000 (contract size) | shares delivered **per contract** | European | Cash | ✅ |
| US ADR option (compare) | point / share | × 100 (US contract) | shares **per contract** | American | Physical | ✅ (short = early-assignment risk) |

## Agent skills

### Issue tracker

Issues live as GitHub issues in this repo (hansel7121/Taiwan-Warrants-App), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root, created lazily as terms/decisions get resolved. See `docs/agents/domain.md`.
