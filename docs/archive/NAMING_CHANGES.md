# NAMING_CHANGES.md

This documents the fetch/refresh/arb-matching rename that landed across Phases
0-4 of a pipeline rework, for whoever reviews this on merge.

## Why this changed

The original fetch/refresh/arb-matching code grew across many separate
prompts and sessions before the author was familiar with the full
route/function surface, which left `fetch` and `refresh` each meaning several
different things depending on context, three near-identical cross-market
matching features with three different names at every layer, and a real bug
where the "Refresh" button silently stopped re-scraping live sources and just
rewrote the existing database snapshot back to itself. This rename makes the
pipeline's vocabulary consistent and, in the process, fixes that bug at its
root — see `git log` for the phase-by-phase commits if you want the detailed
history (look for commits prefixed `phase0`, `phase1`, ... `phase4`).

## New vocabulary

- **`read_X`** — reads the stored Supabase snapshot for display. Never
  scrapes a live source, never writes. Falls back to a one-off live scrape
  (not persisted) only when the snapshot is empty or errors.
- **`scrape_X`** — pulls fresh data from a live source into memory. Never
  touches the database. Source-qualified where a product has one specific
  source (`scrape_cmoney_warrant`, `scrape_cmoney_key`, `scrape_mis_tw_option`,
  `scrape_yfinance_us_option`, `scrape_twse_universe`); the TW-option scraper
  (`scrape_tw_option`) tries two possible sources (MIS primary, TAIFEX EOD
  fallback) internally, so it keeps the generic product name rather than a
  source-qualified one.
- **`sync_X`** — orchestrator: scrape then write the Supabase snapshot. What
  the "Sync now" button triggers. Debounced per-product, 60s. **As of Phase
  3, `sync_X` is synchronous** — the HTTP response only comes back once the
  scrape+write actually completes (previously it dispatched a background
  thread and returned instantly, which is what caused the coupling bug this
  rework fixes at the root).
- **`match_X_Y`** — the three cross-market comparisons: `match_warrant_tw_option`,
  `match_warrant_us_option`, `match_tw_us_option`.
- Products are named `warrant`, `tw_option`, `us_option` throughout (was a mix
  of singular/plural/redundant-qualifier names).

## Routes

| Old | New |
|---|---|
| `POST /fetch` | `POST /read_warrant` |
| `POST /fetch_options` | `POST /read_tw_option` |
| `POST /us_options` | `POST /read_us_option` |
| `POST /download` | `POST /read_warrant_csv` |
| `POST /download_options` | `POST /read_tw_option_csv` |
| *(new — parity gap, didn't exist before)* | `POST /read_us_option_csv` |
| `POST /refresh {kind:"warrants"}` | `POST /sync_warrant` |
| `POST /refresh {kind:"tw_options"}` | `POST /sync_tw_option` |
| `POST /refresh {kind:"us_options"}` | `POST /sync_us_option` |
| `POST /arb_finder` (+ `_csv`) | `POST /match_warrant_tw_option` (+ `_csv`) |
| `POST /us_option_match` (+ `_csv`) | `POST /match_warrant_us_option` (+ `_csv`) |
| `POST /tw_us_option_match` (+ `_csv`) | `POST /match_tw_us_option` (+ `_csv`) |

**Behavior change, not just a rename**: `/sync_warrant`, `/sync_tw_option`,
`/sync_us_option` are now blocking/synchronous (Phase 3) — the old `/refresh`
fired a background thread and responded in well under a second regardless of
market size; the new routes hold the connection open for the real scrape
duration (seconds for US options, up to ~25s for the full warrant universe).
A debounced call (another sync already ran within the last 60s) still returns
immediately, before any blocking work starts.

## Backend functions

**`warrant_logic.py`**: `fetch_warrants()` split into `read_warrant()` (pure
Supabase-first reader, live-fallback) and `scrape_cmoney_warrant()` (pure live
scraper, `force=True` bypasses the in-process cache — this split is the fix
for the coupling bug, see Phase 1). `refresh_cmoney_key()` → `scrape_cmoney_key()`.
`refresh_warrant_universe()` → `scrape_twse_universe()`. `refresh_warrant_cache()`
deleted (dead code, zero callers).

**`options_logic.py`**: `fetch_options()` split into `read_tw_option()` +
`scrape_tw_option()` (tries `scrape_mis_tw_option()` primary, falls back to an
inline TAIFEX EOD path — not extracted into its own named function, since
Phase 2 was a pure-rename phase and that would have been a restructure).
`fetch_options_mis()` → `scrape_mis_tw_option()`. Added a `force=True` bypass
to every in-process TTL cache (`_mis_cache`, `_taifex_cache`, `_spot_cache`)
so the scraper can guarantee a fresh fetch — this didn't exist before Phase 1.
`refresh_cache()` deleted (dead code). Also fixed an asymmetry where an
empty-after-filter Supabase read raised a hard error here but returned
gracefully in `warrant_logic` — both now return an empty result gracefully.

**`us_options_logic.py`**: `fetch_us_options()` split into `read_us_option()` +
`scrape_yfinance_us_option()` (drops its in-process cache's TTL short-circuit
entirely so every call is guaranteed fresh, then repopulates the cache for the
reader side). `refresh_cache()` deleted (dead code). Gained `us_option_last()`
and `us_options_scan()`, relocated here from `arb_logic.py` (Phase 4 — they
only ever operated on US ADR option data and didn't belong in the matching
module).

**`arb_logic.py`**: `build_arb_df()` → `match_warrant_tw_option()`;
`build_us_match_df()` → `match_warrant_us_option()`; `build_tw_us_option_df()`
→ `match_tw_us_option()`. `us_options_scan()` and `us_option_last()` moved out
(see above).

**`services/scheduler.py`**: `refresh_cmkey/universe/warrants/tw_options/us_options()`
→ `sync_cmkey/universe/warrant/tw_option/us_option()`, now calling the pure
`scrape_X` functions directly instead of the old shared `fetch_X` functions —
this is the actual coupling-bug fix (Phase 1); the renames (Phase 2) came
after behavior was already correct. `force_refresh(kind)` (powers all three
`/sync_X` routes) is now synchronous (Phase 3) and its debounce check-then-set
is guarded by a lock (added in a Phase 3 follow-up, after review surfaced a
narrow race where two near-simultaneous requests for the same kind could both
slip past the debounce).

## Frontend (`static/js/*.js`, `templates/index.html`)

`fetchData()` → `readWarrant()`; `refreshWarrants()` → `syncWarrant()`;
`fetchOptionsData()` → `readOption()`; `refreshOptions()` → `syncOption()`
(the TW/US options tabs share one function pair, dispatching on the market
toggle, rather than having four separate functions — that matches how the UI
is actually structured, one tab with a toggle, not two tabs). `runArb()` →
`matchWarrantTwOption()`; `fetchUsData()` → `matchWarrantUsOption()`;
`fetchTwUsData()` → `matchTwUsOption()`. Button copy: "Fetch"/"Refresh now" →
"Read"/"Sync now". UI labels: "Find Arb" → "Warrant vs TW Option"; "Find
Match" (US options tab) → "Warrant vs US Option"; "Find Match" (TW/US tab) →
"TW Option vs US Option".

**Also fixed in Phase 3 (the sync/read workflow)**: `refreshNow()` (`common.js`)
no longer polls `/read_X` every 4 seconds for up to 60s waiting for data to
change (redrawing the table on every tick) — since `/sync_X` is now
synchronous, it's a linear `clear table → await sync → await one read → render`
sequence instead. A follow-up review pass also found and fixed two related
races: the "Read" buttons weren't disabled during an active sync (so a manual
click could race the sync's own trailing read), and switching the TW/US
options market mid-sync could fire a stray read for the wrong market once the
original sync finished. Both are now guarded.

## Output field-schema fix (`match_tw_us_option` only)

`match_tw_us_option` compares a TW-listed option against a US ADR option — no
warrant is involved — but its row output previously used `warrant_*`/`opt_*`-
prefixed keys, inherited from the genuinely-warrant-involving match functions'
schema. Fixed to `tw_option_*`/`us_option_*` (19 fields renamed; see
`logic/arb_logic.py`'s `_match_option_legs()`). The two other match functions
(`match_warrant_tw_option`, `match_warrant_us_option`) keep `warrant_*`/`opt_*`
unchanged, since a warrant genuinely is one leg of those comparisons. The
shared frontend P&L scenario modal (`openArbModal` and everything downstream
of it in `arb.js`) still expects `warrant_*`/`opt_*` internally, since it's
shared by all three tabs — only `openTwUsModal()`'s translation layer (which
builds that modal's input from the raw match row) was updated to read the new
field names on its input side, while still writing the modal's stable
`warrant_*`/`opt_*` slots on its output side.

## Not done in this rework (deferred)

- Moving the TW/US option tracked-product lists (`COMMODITY_MAP`, `US_ADR_MAP`)
  from hardcoded Python dicts to Supabase-backed tables, with admin-gated add
  routes. Warrants' tracked-stock list was already DB-backed (`custom_stocks`)
  and needed no change.
- The Supabase warrant-universe snapshot read-path gap (`warrant_logic._universe()`'s
  fallback chain skips the Supabase snapshot entirely today) and a manual
  `/sync_universe` route.
- Restoring the UI market-session clock (Pacific time, PT/ET ordering, hover
  tooltips) and the performance work (gzip, vectorized IV, concurrent US
  option fetches, corrected pagination) that were rolled back before this
  rework started — both deliberately sequenced after the naming/correctness
  work so any regression is attributable to one change at a time.
- Live per-item sync progress in the browser UI (per-item detail stays in
  server logs only — a past attempt at 1-second polling for this caused real
  log noise and was tied to a batch suspected of causing the original bugs).
