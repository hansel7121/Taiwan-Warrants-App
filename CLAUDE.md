# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this app does

A desktop warrant scanner for Taiwan stock warrants. It runs as a local Flask web server that auto-opens in the browser, fetches live warrant data from CMoney, and displays a filterable table and IV surface chart.

## Commands

**Run in development:**
```bash
python app.py
```

**Before running `python app.py`, always kill any existing instance first:**
```bash
pkill -f "python app.py" 2>/dev/null; lsof -ti:5001 | xargs kill -9 2>/dev/null; echo "cleared"
```
Run this automatically whenever the user asks to run `python app.py`.

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

## Workflow

After finishing any code change, always commit and push to GitHub (`git add`, `git commit`, `git push origin main`) without waiting to be asked.

**Clear server cache / restart app (copy-paste ready):**
```bash
pkill -f "python app.py" 2>/dev/null; lsof -ti:5000 | xargs kill -9 2>/dev/null; sleep 1 && conda run -n godepy python app.py &> /tmp/app_fresh.log & sleep 5 && open http://127.0.0.1:5000/
```

**Hard-refresh browser cache:** `Cmd + Shift + R`

## Taiwan options exercise ratios

All Taiwan equity individual stock options (個股選擇權) on TAIFEX have **exercise_ratio = 2000** shares per contract. Do not change this unless the user explicitly specifies otherwise. TXO (index options) uses 50 NT$/point.

## Key runtime detail

When frozen by PyInstaller (`sys.frozen == True`), the Playwright Chromium binary must be located at `<exe_dir>/ms-playwright/`. The `PLAYWRIGHT_BROWSERS_PATH` env var is set accordingly at import time in `warrant_logic.py`.