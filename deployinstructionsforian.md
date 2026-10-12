# Tab Cleanup + EOD Replay StatArb Ranking: What Changed and How to Deploy

**Branch:** `hansel/cleanup-tabs-statarb-replay` (off `main`, pushed)
**Status:** implemented and tested; **not deployed yet**. Deploying is the 4 steps in [Part 2](#part-2-deploy-on-coolify).

All times in this document are **Taipei time (Asia/Taipei)**.

---

## TL;DR

- **Removed:** the Home tab, the admin/user mode switch (admin is now the only mode), the Arb Finder's **Experiment** subtab, and Live Arb's **Direct Match** and **LP** subtabs. The Live Arb tab is now just **EOD Replay**.
- **Added:** the 13:40 end-of-day replay now also ranks long-warrant / short-option pairs by **how much daily volatility 2330 would need to end in the pair's loss region at the option's expiry**. It keeps the **top 10 per day** and shows them in the EOD Replay tab, under the Direct Match and LP tables.
- **To deploy:** run one SQL migration, merge the PR, and redeploy. No new environment variables, volumes or dependencies. After that it runs on its own every trading day.

---

## Part 1: What changed

### 1.1 Removed (about 4,300 lines)

| Removed | Notes |
|---|---|
| **Home tab** | Markup, `static/js/home.js`, its CSS |
| **Admin/user mode switch** | `services/roles.py`, `static/js/features.js`, every `show_admin` template branch. There is now one mode, and **Options Scanner** is the landing tab |
| **User-mode Dashboard** (positions, alerts, quotes) | It was only reachable in user mode. Removed `static/js/dashboard.js`, the `/list_alerts`, `/add_alert`, `/remove_alert`, `/record_alert_trigger`, `/list_positions`, `/add_position`, `/remove_position`, `/close_position` and `/user_quotes` routes, `logic/user_marks.py`, and their tests. The Supabase tables are **not** touched |
| **Arb Finder → Experiment** | Subtab and the `/match_static_arb(_csv)` routes. The LP solver itself (`logic/static_arb.py`) stays, because the scheduler's suggestions use it. The breakdown modal stays, because Portfolio → Suggestions opens it |
| **Live Arb → Direct Match and LP** | Subtabs, `static/js/live_arb.js`, `static/js/live_arb_lp.js`, `services/live_arb.py`, `services/db_live_arb.py`, `services/db_live_arb_lp.py`, and their routes (`/live_arb_*`, `/start|stop_live_arb[_lp]`, `/live_tick_log_*`) |

**Kept:**

- Watchlist stars on the scanner tables, moved into a new `static/js/watchlist.js`.
- The tick recorder and the EOD replay. The scheduler still starts and stops recording by itself. The manual **Record / Reset CSV** buttons are gone; they lived in the removed subtabs.
- `logic/live_arb_logic.py` and `logic/live_arb_lp_logic.py`, which the replay uses. Their live-only helpers were dropped.

### 1.2 Added: StatArb ranking in the EOD replay

**How it works** (`logic/statarb_logic.py`: `pair_loss_intervals`, `required_daily_vol`, `required_vol_pairs`; `logic/eod_arb_replay.py`):

1. During the 13:40 replay, every time a warrant or option row changes, every long-warrant / short-option pair involving that row is rescored against the latest recorded 2330 spot. This is the same incremental pattern Direct Match uses.
2. Pairs follow the **StatArb pair scanner's rules**:
   - same type;
   - the option expires no later than the warrant;
   - the option bid is live and not crossed;
   - net credit (option bid × 2,000 minus warrant ask × units);
   - the warrant leg is rounded down **and** up to whole 張. The sizing with the higher required vol is kept.
3. The at-expiry payoff (the warrant valued at intrinsic) is piecewise linear, so its **loss region is computed exactly**, not on a grid. A test checks that it matches the StatArb tab's grid version.
4. **Required daily vol = |ln(nearest loss edge / spot)| / √(trading days to option expiry).** This is the daily vol at which ending in the loss region is a one-sigma move. If spot is already in the loss region, it is 0.
5. Each pair keeps its **highest value of the day** (with when that happened). The **top 10** are stored.
6. Pairs with **no loss region** (riskless) are left out. Those are arbs, and Direct Match covers them.

**Needs the spot file.** The ranking uses the day's 2330 spot CSV (`tsmc_spot_YYYYMMDD.csv`, written next to the tick CSV since the 10 Oct change). Days without a spot file still replay Direct and LP normally; they just have no StatArb rows.

**Storage:** new table `eod_arb_statarb_top` (migration `supabase/migrations/028_eod_arb_statarb_top.sql`). It has one row per rank per day, is server-only (RLS on, no policy) like the other EOD tables, and **Re-run** replaces the day's rows. If the table is missing, the replay **does not fail**. It logs a line telling you to apply migration 028 and still stores Direct and LP.

**UI:** EOD Replay tab, new **StatArb pairs** counter and a **"StatArb — top 10 by required daily vol"** table showing:
- rank and peak time;
- the pair, strikes, and DTE (warrant / option / trading days);
- quotes and sizes, lots, credit, and spot;
- the loss region, the move needed to reach it, and the required daily vol.

**Script:** `scripts/eod_arb_replay.py` picks up the spot file by itself. For a hand-run on loose files there is a new `--spot-csv` flag.

**Cost:** about 50% more replay time on the synthetic test day (4.0 s → 6.1 s). On a realistic chain size it measured about 2 ms per option tick.

### 1.3 Testing

- Full suite: **387 passed** with the Rust engine; **359 passed + 28 skipped** on the Python fallbacks.
- New tests:
  - the analytic loss region matches the scanner's grid (calls, puts, open-top losses, debits);
  - the required-vol formula;
  - pair filtering (riskless and debit pairs are skipped);
  - the replay ranking is sorted and capped at 10, with peak ≥ first-seen and empty without spot;
  - spot-file parsing.
- The page renders, and the removed routes return 404.

---

## Part 2: Deploy on Coolify

Nothing new is needed in Coolify: same volume (`/data/live_tick_logs`), same environment variables, no new dependencies. The spot CSV goes into the same `LIVE_TICK_LOG_DIR` volume.

> ⏰ **Timing:** deploy **outside 09:00–14:00** if you can. Mid-session redeploys lose ticks during the downtime, and a redeploy after 13:40 kills that day's replay (fixable with Re-run, see below). Evening or before 08:40 is ideal.

### Step 1: Create the table (Supabase)

1. Open the **Supabase dashboard**, choose the project, and open **SQL Editor**.
2. Paste the **entire** contents of `supabase/migrations/028_eod_arb_statarb_top.sql` and click **Run**.
3. It should say "Success". Under **Table Editor** you should now see `eod_arb_statarb_top`.

### Step 2: Merge the PR

Merge the PR for `hansel/cleanup-tabs-statarb-replay` into `main`. If `main` has moved since, pull `main` into the branch first and resolve conflicts (per CLAUDE.md).

### Step 3: Redeploy

- If Coolify auto-deploys on push to `main`, the merge already started the deploy. Otherwise click **Redeploy**.
- No new packages, so the build should look the same as last time.

### Step 4: Verify

Right after the deploy:

- The app opens on **Options Scanner**. There is no Home tab and no Admin/User mode button.
- The tab bar ends with **EOD Replay**, and Arb Finder has no Experiment subtab.
- Coolify **Logs** show `SCHED: started`.

On the next trading day, the 13:40 replay log should include:

| Log line | Meaning |
|---|---|
| `EOD: YYYY-MM-DD: replaying … (lp=on, spot rows=N)` | The spot file was found. If it says `spot rows=0 — StatArb ranking skipped`, the spot file is missing |
| `EOD: … X Direct + Y LP episodes, Z StatArb scores in …s` | Finished |
| `EOD: … StatArb top 10 not stored … apply supabase/migrations/028…` | Step 1 was skipped. Run it, then Re-run the day |

Then open **EOD Replay**, pick the date, and check the StatArb table.

### Backfilling days already recorded

Any day whose tick file **and** spot file are still on the volume can be ranked after the deploy. Open **EOD Replay**, pick the day, and click **Re-run**. This replaces that day's Direct, LP and StatArb rows. Days recorded before the spot recorder went live have no spot file, so they get no StatArb rows.

---

## Operating notes

- Everything from the previous rollout still applies. The server must be up from 08:40 until the replay ends, don't redeploy during the replay, and use Re-run if a replay was killed.
- **There are no manual recording buttons anymore.** Recording is fully scheduler-driven (08:40 prep, 09:00–13:30 record, 13:31 stop). To check it's working mid-day, look for `LIVETICKLOG: recording to …` in the logs. **EOD Replay → Download ticks / Download spot** gets the files for any listed day.
- Turning off the whole recording + replay feature: set `ENABLE_TICK_RECORDING=0` and redeploy, same as before.

### Testing locally (optional)

```bash
conda activate warrants          # or the repo's .venv
TZ=Asia/Taipei python -m pytest tests -q
RUST_ENGINE=python TZ=Asia/Taipei python -m pytest tests -q

# Replay a real day by hand without touching Supabase (download both files from EOD Replay first)
TZ=Asia/Taipei python scripts/eod_arb_replay.py --csv tsmc_ticks_YYYYMMDD.csv.gz \
    --spot-csv tsmc_spot_YYYYMMDD.csv.gz --dry-run --out result.json
```

---

## File map

**New**
- `static/js/watchlist.js`: scanner watchlist stars (split out of the removed dashboard)
- `supabase/migrations/028_eod_arb_statarb_top.sql`: StatArb top-10 table
- `tests/services/test_watchlist_flow.py`

**Changed**
- `logic/statarb_logic.py`: analytic loss region, required vol, pair scorer
- `logic/eod_arb_replay.py`: spot reading, StatArb scoring per changed row, top-10 records
- `scripts/eod_arb_replay.py`: loads the spot file, stores the ranking, `--spot-csv`
- `services/db_eod_arb.py`: `replace_statarb`, `list_statarb`
- `app.py`: `/eod_arb_episodes` returns `statarb`; removed routes and role gating
- `templates/index.html`, `static/js/live_arb_eod.js`, `static/js/common.js`, `static/js/scanners.js`, `static/js/arb.js`, `static/js/static_arb.js`, `static/css/app.css`
- `services/db_user.py`: watchlist only
- `supabase/schema.sql`, `CLAUDE.md`: docs

**Deleted**
- `static/js/home.js`, `dashboard.js`, `features.js`, `live_arb.js`, `live_arb_lp.js`
- `services/roles.py`, `live_arb.py`, `db_live_arb.py`, `db_live_arb_lp.py`
- `logic/user_marks.py`
- Matching tests
