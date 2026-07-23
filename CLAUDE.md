# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow

- **Never commit or push directly to `main`.** Two people work on this repo; direct pushes to `main` from multiple people is what causes conflicts.
- Before starting any new work, pull `main` first (`git pull origin main`) so you're branching from the latest code. A `SessionStart` hook already runs this automatically at the start of every session.
- Create a feature branch off `main` for the change (e.g. `yourname/short-description`), commit and push to that branch as you go — don't batch many changes into an unpushed pile.
- Open a PR (`gh pr create`) instead of pushing to `main` directly. Before merging, pull `main` into the branch again and resolve any conflicts, since a teammate may have pushed to `main` directly in the meantime.
- **Commit message format:** `type: short summary` (`feat`, `fix`, `refactor`, `docs`, `chore`). No trailing period on the summary line. Use the body to explain *why*, not what — the diff already shows what changed.
- **Do NOT add Claude / the assistant as a contributor.** Never put a `Co-Authored-By: Claude ...` (or any `Co-Authored-By` / author trailer pointing at the assistant or `noreply@anthropic.com`) in commit messages. Commits must be authored by the human only. This overrides any default that would append such a trailer.

## What this app does

A desktop warrant scanner for Taiwan stock warrants. It runs as a local Flask web server that auto-opens in the browser, fetches live warrant data from CMoney, and displays a filterable table and IV surface chart.

## Commands

**Run in development:**
```bash
python app.py
```

**Build standalone executable (PyInstaller):**
```bash
pyinstaller app.spec
# Output: dist/app
```

**The conda environment used for builds:**
```
/opt/miniconda3/envs/godepy
```
Use `conda activate godepy` before running or building if not already active.

## Architecture

The app has two layers:

**`warrant_logic.py`** — all data logic:
- Uses `twstock` to look up which warrant codes are associated with a given stock ticker
- Fetches live pricing from CMoney's private API (`mainpage.ashx`). CMoney requires a session-scoped `cmkey` token that is extracted by launching a headless Playwright/Chromium browser and intercepting network requests to `warrantsquery.aspx`. This key is prefetched in a background thread at startup.
- Parallel fetches (up to 100 workers) for individual warrant data via `ThreadPoolExecutor`
- Computes Black-Scholes IV (via Brent's method), delta, and leverage from raw CMoney prices
- Auto-refreshes the cmkey if CMoney returns error `-3` (key expired)

**`app.py`** — Flask routes:
- `POST /fetch` — returns filtered warrant rows as JSON
- `POST /download` — returns filtered data as CSV
- `POST /iv_surface` — returns interpolated IV surface (80×80 grid via `scipy.griddata`) plus scatter points
- `/get_custom_stocks` and `/save_custom_stocks` — persist a user's custom stock list to `custom_stocks.json` next to the executable

**`templates/index.html`** — single-page frontend:
- Two tabs: warrant table and IV surface (Plotly 3D surface)
- Filter controls: option type, days to expiry, min leverage, max time-value %, min volume
- Multi-select stock list with custom stock management (add/remove, persisted via API)
- Table has sticky headers and supports CSV download

## Key runtime detail

When frozen by PyInstaller (`sys.frozen == True`), the Playwright Chromium binary must be located at `<exe_dir>/ms-playwright/`. The `PLAYWRIGHT_BROWSERS_PATH` env var is set accordingly at import time in `warrant_logic.py`.

## Taiwan warrant & option mechanics

These are the domain rules that govern all pricing, sizing, and P&L math in this app. Get these wrong and every arb/hedge number is off.

### Units and board lots

- A warrant's smallest tradable *unit* is 1 warrant, but warrants **trade only in board lots (張) of 1,000 units**. So any quantity of warrants that must actually be executed has to be a whole number of board lots.
- To convert a raw units figure into board lots (張): **divide units by 1,000**. A fractional 張 (e.g. 3.472 張) is a theoretical/exact hedge size; a real executable order must be rounded to a whole 張, which leaves residual delta.

### Warrant pricing

- Warrants are **priced per unit**. A quote of `P` NTD means one *unit* costs `P` NTD.
- Therefore one **board lot costs `P × 1,000` NTD** (per-unit price × 1,000 units).

### Exercise ratio (warrants)

- The **exercise ratio is the number of underlying shares delivered per single warrant *unit*** (not per board lot). It is often fractional (e.g. 0.5 shares/unit).
- Consequences:
  - One board lot (1,000 units) delivers `1,000 × exercise_ratio` shares.
  - **Price per underlying share** = `warrant_unit_price / exercise_ratio` (this is how the app normalizes warrant prices for put-call parity comparisons — see `app.py:291`).
  - **Units needed to cover `N` shares** = `N / exercise_ratio`; board lots needed = `(N / exercise_ratio) / 1,000` (see `app.py:323`, `warrants_needed = opt_contract_size / ratio`).
- ⚠️ Common mistake: exercise ratio is **per unit, not per board lot**. A board lot does *not* deliver `exercise_ratio` shares total — it delivers `1,000 × exercise_ratio` shares.

### Warrant style, settlement & the short constraint

- Taiwan warrants are **cash-settled** — exercise/expiry pays out cash against the settlement price; the holder does **not** receive actual underlying shares.
- Warrants are **American-exercisable** (holder may exercise before expiry) but in practice are traded (sold), not exercised — so pricing treats them like tradable American-style cash-settled contracts.
- ⚠️ **Warrants are LONG-ONLY. You cannot write or short a warrant.** A retail/desk participant can only *buy* warrants (open long) and later sell to close. Any arb or hedge structure that would require a **short warrant leg is not executable** — only strategies where the warrant is held long are valid. This is a hard constraint on every warrant-side arb.

### Equity options (for comparison)

- Taiwan single-stock equity options always have an **exercise ratio / contract size of 2,000 shares** per contract.
- Options are **priced per point (per share), not per unit**: a quote of `2` NTD is worth `2 × 2,000 = 4,000` NTD for one contract (price × contract size). See the arb modal, which multiplies option price by `opt_contract_size` (2,000).
- **Taiwan equity options are European style** — exercisable only at expiry, never early.
- Taiwan equity options are **cash-settled** — settled in cash against the final settlement price at expiry, no physical share delivery.
- Because TW options are European + cash-settled, a **short TW option leg carries no early-assignment risk**. Contrast the US ADR options, which are **American** (early-exercisable): a short US leg *does* carry early-assignment risk. This asymmetry matters for every TW/US comparison.

### Quick reference

| Instrument | Priced per | Multiplier to get one lot/contract cost | "Ratio" meaning | Style | Settlement | Short allowed? |
|---|---|---|---|---|---|---|
| Warrant | unit | × 1,000 (units per 張) | shares delivered **per unit** | American-exercisable (traded, not exercised) | Cash | ❌ Long-only — cannot write/short |
| TW equity option | point / share | × 2,000 (contract size) | shares delivered **per contract** | European | Cash | ✅ |
| US ADR option (compare) | point / share | × 100 (US contract) | shares **per contract** | American | Physical | ✅ (short = early-assignment risk) |