# Taiwan Warrant & Options Scanner

A local web app for scanning Taiwan stock warrants and equity options. It fetches live market data, computes implied volatility, and surfaces potential arbitrage opportunities between warrants and TAIFEX options.

---

## What It Does

The app runs as a local Flask server that auto-opens in your browser. It has four main tabs:

### 1. Warrant Scanner
Fetches live warrant data from CMoney for any supported Taiwan stock. For each warrant it computes:
- **Implied Volatility (IV)** — Black-Scholes IV solved via Brent's method on the ask price
- **Delta** — Black-Scholes delta
- **Real Leverage** — (underlying price × delta) / warrant ask
- **Time Value** and **Time Value %** — extrinsic value as a fraction of the underlying price

Filterable by: option type (Call/Put/All), days to expiry, min leverage, max time value %, min volume.

### 2. IV Surface
Renders a Plotly 3D surface of the warrant IV across strike and DTE. Useful for spotting which warrants are priced rich or cheap relative to the surface.

### 3. Options Scanner
Fetches live TAIFEX equity option data (bid/ask from the last-best-price columns) for supported stocks. Computes IV, delta, and leverage for each contract. Shows whether each quote is a live bid/ask or a settlement-price fallback.

### 4. Arb Finder
Two modes for finding pricing inconsistencies between warrants and options:

#### Direct Match
Pairs each warrant against a same-type option on the same underlying and looks for violations of **call/put monotonicity** — the rule that a higher-strike call must be cheaper than a lower-strike call.

- **Positive price_diff** — the option (higher strike) is more expensive than the warrant (lower strike). Trade: buy warrant, sell option. Entry credit is locked in regardless of where the stock ends up at expiry.
- **Negative price_diff** — the warrant (higher strike) is more expensive than the option (lower strike). Trade: buy option, sell warrant (monitoring only — warrants cannot be shorted in practice).

**Filters:**
- **Max Strike Diff %** — maximum strike difference between the warrant and option as a % of the warrant strike
- **Max DTE Diff (days)** — one-sided cap on the *unfavorable* DTE gap only:
  - For positive direction: caps the days the warrant expires *before* the option (the risky side). If the warrant expires after the option, the gap is unbounded — extra warrant time value only helps.
  - For negative direction: caps the days the option expires *before* the warrant. The safe side is unbounded.

Clicking any row opens a **trade breakdown modal** showing:
- Entry cash flows for each leg at executable bid/ask prices
- A comparison table with IV for both instruments
- A **P&L chart** with two traces:
  - *Dotted line* — theoretical P&L at expiry (intrinsic value only), always ≥ 0 for valid arb pairs
  - *Green line* — mark-to-market P&L at the DTE shown on the slider, using each instrument's Black-Scholes value. Can dip mid-life if IV spreads are large, but the expiry payoff is unaffected.

#### PCP Match (Put-Call Parity)
Uses put-call parity to identify mispricing between call and put warrants via synthetic replication.

---

## Supported Stocks

| Code | Name | Option IDs |
|------|------|-----------|
| 2330 | 台積電 (TSMC) | CDA, CDO |
| 2303 | 聯電 (UMC) | CCO |
| 2603 | 長榮 | CZA, CZO |
| 2881 | 富邦金 | CEO |
| 2882 | 國泰金 | CKO |
| TXO | TAIEX Index | TXO |

---

## Architecture

```
app.py                Flask routes (request/response only)
wsgi.py               gunicorn entry point (wsgi:app)
logic/                pure market-data + math, no side effects
  warrant_logic.py      CMoney data fetch, IV/delta/leverage computation
  options_logic.py      TAIFEX option data fetch and computation
  us_options_logic.py   US ADR option data fetch and computation
  arb_logic.py          warrant<->option matching, put-call parity, TW/US leg arb
services/             side-effecting infrastructure
  db.py                 Supabase client
  store.py              portfolio persistence (Supabase + local JSON mirror)
  auth.py               JWT verification + email allowlist
  scheduler.py          APScheduler cache-refresh jobs (opt-in)
  applog.py memlog.py   request + memory logging
templates/
  index.html          single-page frontend shell (Jinja + Plotly)
static/
  css/app.css           extracted stylesheet
  js/*.js               extracted JS bundles (common, quant, scanners, arb, portfolio)
```

**Data sources:**
- **Warrants** — CMoney private API (`mainpage.ashx`), requires a `cmkey` token scraped from the warrantsquery page HTML (plain HTTP, no browser)
- **Options** — TAIFEX public CSV download (`optDataDown`)
- **Spot prices** — TWSE MIS API, Yahoo Finance fallback, yfinance fallback

**Key computations:**
- IV solved with Brent's method (bounds `[1e-6, 5.0]`)
- Black-Scholes delta with continuous risk-free rate (Taiwan CBC benchmark, 1.875%)
- All Taiwan equity options use exercise ratio = 2,000 shares/contract
- TXO index options use 50 NT$/point

**Notebooks:** `notebooks/` holds exploratory research (ADR parity, universe
screening). Committed **without outputs** — clear cell outputs before committing
(`jupyter nbconvert --clear-output --inplace notebooks/*.ipynb`, or the jq one-liner
in the git history) to keep diffs small.

---

## Running Locally

```bash
# Requires Python 3.11+ (conda env: warrants)
conda activate warrants
pip install -r requirements.txt

# Dev server — always prefix with the Taiwan timezone (see note below)
TZ=Asia/Taipei python app.py                 # http://127.0.0.1:5001

# Production-style (what deployment uses)
TZ=Asia/Taipei gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:5001 wsgi:app
```

Stop the dev server with **`Ctrl+C`** in that terminal (or `pkill -f "python app.py"`).

**Always set `TZ=Asia/Taipei`.** The scanners compute days-to-expiry against the
machine's local "today". On a machine west of Taiwan (e.g. US Pacific) the local
date is a day behind Taiwan for part of the day, so an already-expired option
batch leaks into the results and counts come out inflated (e.g. ~1580 locally vs
~1407 on the UTC deploy). The `TZ` prefix scopes Taiwan time to this process only
— it does not change your computer's clock.

**No-login local mode.** With `LOCAL_USER_ID` set in `.env` and `RENDER` unset, the
app runs with no login and acts as that fixed user, syncing its portfolio with the
shared Supabase. `.env` lives at the **repo root** (loaded by `services/db.py`). See
[`SETUP-LOCAL.md`](SETUP-LOCAL.md) for the full local-redundancy setup and seeding steps.

Exactly **1 gunicorn worker**: market-data caches live in process memory and would diverge across workers; threads provide concurrency.

**macOS only:** if gunicorn workers die with `objc ... fork()` errors on routes that hit yfinance, prefix the command with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`. Linux deployments are unaffected.

---

## Deploy (Render)

The repo ships a `render.yaml` blueprint. In the Render dashboard, create a new **Blueprint** from this repo — it provisions a native Python web service that runs gunicorn with a single worker.

After the service is created, set these three secrets in the Render dashboard (they are marked `sync: false`, so Render prompts for them — never commit real values):

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`

The blueprint uses the **starter** plan; the free tier's 512 MB is tight for the pandas/numpy/scipy working set.

**Post-deploy:** add the live Render URL (e.g. `https://<service>.onrender.com`) to your Supabase project's **Auth → URL Configuration → Redirect URLs**, or magic-link sign-in will fail.

---

## Options Exercise Ratios

All Taiwan individual equity options (個股選擇權) on TAIFEX use **2,000 shares per contract**. TXO (index) uses **50 NT$/point**.
