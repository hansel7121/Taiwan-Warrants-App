# Performance Baseline — Pre-Phase 8

**Date:** 2026-07-21
**Git commit:** 18a6ae2756420967eca3a6ac5ed1d6b27d356509
**Run:** local dev (`TZ=Asia/Taipei python app.py`, port 5001, no-login mode), warrants conda env.

This captures a "before" snapshot of timing + memory for the routes/functions Phase 8
will change (gzip responses, vectorized IV solve, concurrent US option chain fetch,
memory-safe pagination rewrite). Diff a later "after" run against this to quantify gains.

> **Note:** `cg_peak` (cgroup memory.current) shows `-` locally — it is a Render-only
> signal and is not meaningful on a laptop. RSS figures below are the local signal.
>
> **gzip caveat:** memlog measures duration/RSS only, not response byte size, so the
> gzip-response change in Phase 8 is NOT captured by any line here — it must be measured
> separately (e.g. response `Content-Length` before/after).

## MEM: lines (one per measured task)

| task           | dur    | rss_before | rss_after | rss_peak | notes |
|----------------|--------|-----------|-----------|----------|-------|
| cmkey_http     | 0.6s   | 195.9MB   | 201.3MB   | 201.3MB  | CMoney key fetch, at boot |
| warrants_fetch | 27.2s  | 210.4MB   | 331.0MB   | 365.6MB  | main warrant scrape (relevant to vectorized IV) |
| warrants       | 33.1s  | 210.3MB   | 350.3MB   | 365.6MB  | full `_job('warrants')` wrapper |
| tw_options     | 17.0s  | 310.1MB   | 355.7MB   | 355.7MB  | `_job('tw_options')` (relevant to vectorized IV) |
| us_options     | 2.6s   | 355.7MB   | 355.7MB   | 355.8MB  | `_job('us_options')` (relevant to concurrent US chain fetch) |

`warrant_universe` (ISIN scrape) was intentionally NOT triggered — slow (2-6 min/market),
not relevant to Phase 8.

## SCHED: timing lines (from scheduler.py)

```
SCHED: warrants ok in 33.1s
SCHED: tw_options ok in 17.0s
SCHED: us_options ok in 2.6s
```

## Route responses

- `POST /sync_warrant`   → 200, `{"ok":true,"started":["warrants"],"skipped":[]}`, 33.1s
- `POST /sync_tw_option` → 200, `{"ok":true,"started":["tw_options"],"skipped":[]}`, 17.0s
- `POST /sync_us_option` → 200, `{"ok":true,"started":["us_options"],"skipped":[]}`, 2.6s

`us_options` is legitimately fast here: the local run fetched a small ADR universe
(e.g. UMC 239 contracts / 5 expiries, CHT 12 contracts / 3 expiries). The concurrent-fetch
change should still be visible on a larger universe / on Render.

## Caveats for the "after" diff

- Local `MARKET_SOURCE=supabase`; results/timing can differ from Render.
- Absolute RSS is machine- and process-warmup-dependent; compare deltas (rss_after − rss_before)
  and peaks, not raw numbers, and re-run "after" under the same conditions.
- Booting via `app.py` (not `wsgi.py`), so the `MEM: baseline tag=boot` line from `wsgi.py:12`
  does not appear in this run; the boot-time `cmkey_http` measure did fire.
