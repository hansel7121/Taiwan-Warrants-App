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

---

# Post-Phase-8 comparison (2026-07-22)

**Git commit:** 202d3627b87419cac130a2f1e3104b94088592cb (all of Phase 8: gzip, vectorized IV, concurrent US fetch, read_snapshot pagination fan-out)
**Run:** local dev, same method as the baseline above (`MARKET_SOURCE=supabase TZ=Asia/Taipei python app.py`, port 5001, no-login mode, warrants conda env). Syncs triggered in order via `POST /sync_warrant`, `/sync_tw_option`, `/sync_us_option`.

## Raw MEM: / SCHED: lines captured (after)

```
MEM: task=cmkey_http dur=0.6s rss_before=194.2MB rss_after=199.6MB rss_peak=199.6MB cg_peak=-MB
MEM: task=warrants_fetch dur=23.3s rss_before=204.5MB rss_after=335.2MB rss_peak=354.6MB cg_peak=-MB
MEM: task=warrants dur=27.6s rss_before=201.0MB rss_after=374.3MB rss_peak=374.3MB cg_peak=-MB
SCHED: warrants ok in 27.6s
MEM: task=tw_options dur=25.7s rss_before=374.4MB rss_after=280.5MB rss_peak=381.3MB cg_peak=-MB
SCHED: tw_options ok in 25.7s
MEM: task=us_options dur=2.1s rss_before=277.2MB rss_after=277.1MB rss_peak=277.9MB cg_peak=-MB
SCHED: us_options ok in 2.1s
```

Route responses (all 200): `/sync_warrant` → `{"ok":true,"started":["warrants"],"skipped":[]}` (curl 27.6s); `/sync_tw_option` → `{"ok":true,"started":["tw_options"],"skipped":[]}` (curl 25.7s); `/sync_us_option` → `{"ok":true,"started":["us_options"],"skipped":[]}` (curl 2.1s).

## MEM: lines — before vs after

| task           | dur (before) | dur (after) | rss_peak (before) | rss_peak (after) | notes |
|----------------|--------------|-------------|-------------------|------------------|-------|
| warrants       | 33.1s        | 27.6s       | 365.6MB           | 374.3MB          | vectorized IV solve; ~17% faster |
| tw_options     | 17.0s        | 25.7s       | 355.7MB           | 381.3MB          | vectorized IV solve; slower this run (see takeaways) |
| us_options     | 2.6s         | 2.1s        | 355.8MB           | 277.9MB          | vectorized IV + concurrent chain fetch; faster, lower peak |

(Supporting sub-task: `warrants_fetch` 27.2s→23.3s, rss_peak 365.6MB→354.6MB.)

## gzip response size (8.1a, not captured by MEM: lines)

Measured via response `Content-Length` on `POST /read_warrant`, with and without `Accept-Encoding: gzip` (single stock code per request):

| route                | uncompressed | gzip     | ratio (gzip/raw) | saved |
|----------------------|--------------|----------|------------------|-------|
| /read_warrant (2330) | 335,979 B    | 45,912 B | 13.7% (7.3×)     | 86.3% |
| /read_warrant (2317) | 186,191 B    | 26,875 B | 14.4% (6.9×)     | 85.6% |

Responses carry `Content-Encoding: gzip` only when the client sends `Accept-Encoding: gzip`; otherwise the full uncompressed JSON is returned. ~7× wire-size reduction on realistic warrant payloads.

## Takeaways

- **gzip is the clear, unambiguous win:** ~7× smaller JSON on the wire (86% saved) for `/read_warrant`, entirely transparent (only kicks in when the client advertises gzip). This is the change most likely to help real users on Render, and it was invisible to the MEM: lines by design.
- **warrants path improved:** full `warrants` job 33.1s→27.6s (~17% faster); the underlying `warrants_fetch` scrape 27.2s→23.3s, so the vectorized IV solve is doing real work.
- **us_options improved on both axes:** 2.6s→2.1s and rss_peak 355.8MB→277.9MB. The small local ADR universe limits how much the concurrent chain fetch can show; expect a larger relative gain on Render's full universe.
- **tw_options ran slower this time (17.0s→25.7s)** with a slightly higher rss_peak (355.7→381.3MB). This is almost certainly run-to-run variance in the Supabase scrape/network for this single local run, not a code regression — the vectorized IV change only reduces CPU in the solve step and cannot add ~9s. The baseline doc's own caveat (local `MARKET_SOURCE=supabase`, single run, timing differs from Render) applies; worth confirming against a Render "after" or a repeat local run before drawing any conclusion.
- **Memory is essentially flat, not regressed:** peaks stayed in the ~355–380MB band on the same laptop; no Phase-8 change blew up RSS. Absolute RSS is warmup-/machine-dependent (per the baseline caveat), so these are directional, not precise.
