# Daily TSMC Tick Recording + End-of-Day Arb Replay: What Changed and How to Deploy

**Branch:** `hansel/eod-arb-replay` (2 commits on top of `main` @ `a17a717`, pushed, merges cleanly)
**Status:** implemented and tested; **not deployed yet**. Deploying is the 4 steps in [Part 2](#part-2--deploy-on-coolify).

All times in this document are **Taipei time (Asia/Taipei)**.

---

## TL;DR

Once this is deployed, the server does the following by itself every weekday. Nobody's computer needs to be on, because it all runs inside the Coolify container on the Hetzner box:

| Time | What happens |
|---|---|
| 08:40 | Logs into Fubon and subscribes **every** TSMC (2330) warrant (~1,050) and option |
| 09:00–13:30 | Records every order-book tick (with strike, exercise ratio, expiry, DTE, bid/ask/sizes) to a daily CSV |
| 13:31 | Stops recording |
| 13:40 | Replays the whole day through **Direct Match** and the **LP arb finder** (Python implementations), checking for arbs after every tick. It logs every arb **episode** (start time, end time, how long it lasted, the legs, the edge at open and at peak) to Supabase |
| After replay | Gzips the day's CSV and keeps it 90 days (or up to 20 GB). The arbs in Supabase are kept forever |

The results show up in a new subtab: **Live Arb → EOD Replay**.

To deploy: run one SQL migration, merge the PR, then in Coolify add one volume and one environment variable and redeploy. Details below.

---

## Part 1: What was done in this session

### 1.1 Git housekeeping (on the Web repo)

- Pulled `origin/main` into both local repos (`Taiwan-Warrants-App` and `Taiwan-Warrants-Web`). The Web repo's branch `hansel/live-tick-csv-recorder` had its own always-on tick recorder (`services/tick_recorder.py`), which overlapped with the one already on `main` (`services/live_tick_log.py`, PRs #103/#104).
- Hansel chose to keep **main's** recorder. I reverted the branch's recorder, so that branch is now identical to `main` and **can be deleted**.
- All new work is on a fresh branch off `main`: **`hansel/eod-arb-replay`**.

### 1.2 The feature: automatic recording + end-of-day replay

#### Recording (`services/live_tick_log.py`, `services/live_warrant.py`, `services/live_options.py`)

- The recorder now runs **automatically** (it used to be manual via the Record button, which still works).
- Writes are **buffered** and flushed once a second, instead of a disk flush on every tick. That matters for a ~1,500-instrument chain.
- The tick-log directory is configurable via `LIVE_TICK_LOG_DIR`, so it can live on a **persistent volume** (the container disk is wiped on every redeploy).
- Besides live websocket ticks (`src=ws`), the CSV now also records:
  - `src=snapshot`: every tracked book's full state, written whenever recording starts or resumes (e.g. after a mid-day redeploy). The replay always starts from complete state.
  - `src=rest`: the REST quote that seeds a book before its first tick.
  - `src=terms`: a row when a warrant's strike/ratio/maturity arrive late.
- Snapshot rows drop prices whose book was last updated on a **previous** day. Taiwan orders are day orders, so yesterday's quote isn't really on the book.
- The recorder flushes on shutdown (SIGTERM in `wsgi.py`), so a redeploy loses no buffered rows.
- New helpers: `compress(date)` (gzips a finished day) and `prune()` (retention: `TICK_LOG_KEEP_DAYS`, default 90; `TICK_LOG_MAX_GB`, default 20).

#### Scheduler (`services/scheduler.py`), Mon–Fri

| Job | When | What it does |
|---|---|---|
| `tick_prepare` | 08:40 | Connects both Fubon sessions; subscribes TSMC's whole warrant chain (`scan_underlying("2330", 0)`) and option chain (`load_chain`) |
| `tick_record` | every 5 min, 09:00–13:30 | Starts the recorder if it isn't running, and writes a snapshot when it starts. Reloads the option chain if a restart lost it |
| `tick_stop` | 13:31 | Stops the recorder |
| `eod_replay` | 13:40 | Launches `scripts/eod_arb_replay.py` as a **separate process** (heavy CPU work stays off the web server), then gzips and prunes the CSVs |

All of this can be switched off with the environment variable `ENABLE_TICK_RECORDING=0`.

#### The replay engine (`logic/eod_arb_replay.py`, new)

- Reads the day's CSV and folds every tick into a per-instrument order book. Ticks with the same timestamp are applied together, so a market maker's multi-quote update is never scanned half-applied.
- After every change it re-runs:
  - **Direct Match** using the Python kernel (`arb_kernels_py.direct_pairs`). It runs incrementally: a tick on one warrant only re-pairs that warrant against all options, and vice versa. That's mathematically identical to a full rescan (a test proves it) but ~1,000× cheaper.
  - **LP arb finder** using the Python solver (`static_arb._solve_horizon`, scipy/HiGHS): the same algorithm as the Arb Finder's LP tab.
- Only ticks between 09:00 and 13:30 are scanned. Anything still open at 13:30 is closed and flagged `open_at_close`.
- Turns "which arbs exist right now" into **episodes**:
  - **Direct episode** = one warrant/option pair, from the moment it becomes an arb until it stops. If it reappears later, that's a new episode.
  - **LP episode** = "some riskless structure exists at this option expiry", from appearing to disappearing. The solver keeps swapping irrelevant legs in and out while the same mispricing persists, so keying on exact legs shattered one arb into ~30 episodes. Each LP episode stores the structure at open, the structure at peak, and every instrument used.
- A day with **zero** real websocket ticks (e.g. a Taiwan holiday, which the weekday-only market gate doesn't know about) is logged as `no_ticks` with no episodes.

#### Making the Python LP fast enough (`logic/lp_screen.py`, new)

- Problem: on the full TSMC chain the Python LP takes **~1 second per option expiry**, and one tick can touch ~7 expiries. A real day would take *days* to replay.
- Fix: one persistent, warm-started HiGHS model per expiry that holds the LP relaxation. Each tick updates only the changed instrument's price/depth.
  - The full solver only ever finds an arb when this relaxation is positive, so most ticks are settled by the screen alone.
  - Many ticks need no solve at all. The LP is homogeneous, so if there was no arb and a tick only made prices worse for us (or only changed a resting size), there still isn't one.
- Result: about **20 ms per tick** on a full-size chain, down from ~83 ms with the screen alone and seconds without it. That's roughly 2–6 hours for a real day, well before the next open.
- A test proves the screen produces **exactly the same episodes** as running the full solver every time.
- New dependency: **`highspy==1.15.1`** (added to `requirements.txt`).

Smaller supporting changes:
- `logic/live_arb_logic.py`: `scan()` takes an optional `pairs_fn`, used to force the Python kernel.
- `logic/live_arb_lp_logic.py`: added `engine="python"` and a per-horizon `scan_horizon()`. The live Rust path is unchanged; I checked that Rust and Python give identical structures.

#### Storage (Supabase): migration `supabase/migrations/027_eod_arb_episodes.sql`

| Table | One row per |
|---|---|
| `eod_arb_runs` | trading day: status (`running`/`ok`/`no_ticks`/`error`), tick count, runtime, error |
| `eod_arb_direct_episodes` | Direct Match episode: warrant/option, strikes, DTEs, start/end/duration, warrant ask and option bid (with sizes), edge per share and %, peak edge and when |
| `eod_arb_lp_episodes` | LP episode: horizon, legs at open, legs at peak, every instrument used, net credit, guaranteed profit, return %, peak, start/end/duration |

These are server-only (RLS on, no policy), the same pattern as every other table. Re-running a day deletes and re-inserts that day's rows, so it's safe to repeat. `supabase/schema.sql` is updated too.

#### UI: Live Arb → **EOD Replay** subtab (`static/js/live_arb_eod.js`, `templates/index.html`)

- A date picker covering every recorded or replayed day, with that day's run status (ticks, runtime, errors).
- A Direct Match episodes table and an LP episodes table; click an LP row to see its peak structure and every leg used.
- **Re-run** replays a day again from its tick file. **Download ticks** gets that day's CSV/CSV.gz.
- The existing Direct Match and LP subtabs (with their own Start/Stop switches) are unchanged.

New routes (all `require_auth` + admin): `/eod_arb_dates`, `/eod_arb_episodes?date=`, `POST /eod_arb_run`, `/eod_tick_csv?date=`.

### 1.3 Testing

**`scripts/gen_fake_ticks.py`** generates synthetic trading days in exactly the recorder's CSV format. The chain is 12 options + 15 warrants, priced with Black-Scholes with realistic bid/ask spreads, and warrants priced rich (as in reality):

- **No-arb day**: arbitrage-free by construction.
- **Arb day**: the same day, plus injected arbs at known times.

Replaying both full days through the real CLI (`scripts/eod_arb_replay.py --dry-run`):

| Injected arb | Detected |
|---|---|
| none (no-arb day) | **0 Direct, 0 LP** ✅ |
| Direct: warrant 030001 vs CDAJ6C1000, 10:15:03–10:15:45 | Direct + LP, exactly 10:15:03 → 10:15:45 (42 s) ✅ |
| same pair reopening 11:02:10–11:02:30 | logged as a **separate** 20 s episode ✅ |
| LP-only: option-only vertical/calendar 12:00:00–12:02:00 (Direct can't see it) | LP only, 12:00:00 → 12:02:00 ✅ |
| Direct: warrant 030009 vs puts, from 13:25 to the close | 13:25:00 → 13:30:00, flagged `open_at_close` ✅ |

Test suite: **396 passed** with the Rust engine, and **368 passed + 28 skipped** on the Python fallbacks. New tests:
- `tests/logic/test_eod_arb_replay.py`: the scenarios above, incremental Direct = full rescan, screen = always-solve, episode logic, holiday days, out-of-session ticks.
- `tests/services/test_live_tick_log.py`: buffering, resume, gzip, retention, stale quotes.

I also checked the new subtab in a real browser against the fake day's results.

### 1.4 Known limitations (worth knowing)

1. **Pre-existing LP rounding limitation.** It lives in `static_arb._solve_horizon`, the same algorithm the Arb Finder's LP tab uses and the Rust kernel mirrors. When a leg has very thin depth (e.g. 1 lot), the "round to whole lots, evict the worst leg, retry" heuristic can throw away a simple valid arb. On the fake day this split one LP arb into 3 pieces with short gaps. It wasn't introduced here; the replay was asked to use the existing algorithm. Fixing it properly would mean an exact integer (MILP) solve.
2. **Real runtime is unknown until the first real day.** The estimate is 2–6 h for the full chain (Hetzner may be slower than a Mac). Check the runtime shown in the EOD Replay subtab after day 1.
3. **Holidays:** the gate is weekday-only. On a holiday the jobs still run, but the day is logged as `no_ticks` (harmless).
4. **Pre-open quotes:** quotes from earlier the same morning (pre-open) are kept. Quotes from previous days are dropped.
5. **Clicking Disconnect** on the Live Warrant tab mid-day stops warrant ticks until the next 08:40 job. **Reset CSV** on Live Arb deletes the file the replay uses; don't click it during the day.

---

## Part 2: Deploy on Coolify

The app runs as a **single Coolify container on the Hetzner server**, which is always on. Once these steps are done, everything happens on its own every trading day, and no laptop needs to be open.

> ⏰ **Deadline:** to capture a full trading day, finish all 4 steps **before 08:40 Taipei** that morning. If you're later than that, see [Starting mid-day](#starting-mid-day-deployed-after-0840).

### Step 1: Create the database tables (Supabase)

1. Open the **Supabase dashboard**, choose the project, and open **SQL Editor**.
2. Paste the **entire** contents of `supabase/migrations/027_eod_arb_episodes.sql` and click **Run**.
3. It should say "Success". Under **Table Editor** you should now see `eod_arb_runs`, `eod_arb_direct_episodes` and `eod_arb_lp_episodes`.

Do this **before** deploying. The 13:40 replay writes to these tables and marks the run `error` if they don't exist.

### Step 2: Merge the code to `main`

1. Open https://github.com/hansel7121/Taiwan-Warrants-App/pull/new/hansel/eod-arb-replay
2. Click **Create pull request**, then **Merge pull request**. It merges cleanly; `main` hadn't moved as of writing. If someone has pushed to `main` since, pull `main` into the branch first, per CLAUDE.md.

### Step 3: Configure Coolify (do this before redeploying)

Open the app in Coolify.

**a) Add a persistent volume** for the tick CSVs. Without it, every redeploy wipes that day's ticks.

- Go to **Persistent Storage**, then **+ Add**, then **Volume Mount**.
- **Name:** `tick-logs`
- **Destination path:** `/data/live_tick_logs`
- Save.

**b) Set the environment variables:**

- Go to **Environment Variables** and add or confirm:

| Variable | Value | Required? |
|---|---|---|
| `LIVE_TICK_LOG_DIR` | `/data/live_tick_logs` | **Yes** (must match the volume path above) |
| `ENABLE_SCHEDULER` | `1` | **Yes.** If it's missing, *nothing* runs, and nothing tells you |
| `TICK_LOG_KEEP_DAYS` | `90` | Optional (default 90) |
| `TICK_LOG_MAX_GB` | `20` | Optional (default 20; the CX23 disk is 40 GB) |
| `ENABLE_TICK_RECORDING` | `0` | Only to **turn the whole feature off** |

The existing `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` stay as they are. **Fubon credentials need nothing new**: the server already reads them from Supabase, the same way the Live Warrant tab does today, so it logs in unattended.

You don't need to set `TZ=Asia/Taipei`; the `Dockerfile` already sets it.

### Step 4: Deploy

- If Coolify auto-deploys on push to `main`, the merge in Step 2 already started a deploy. **Redeploy anyway after Step 3** so the volume and variables take effect.
- Otherwise click **Redeploy**.
- Watch the **build log**. `highspy` is a new dependency, so confirm the `pip install -r requirements.txt` step succeeds.

### Step 5: Verify

Right after the deploy, in Coolify **Logs**:

```
SCHED: started
ENGINE: ...
```

On the next trading day, look for these lines in order:

| Time | Log line |
|---|---|
| 08:40 | `SCHED: tick prep warrants chain=… +… failed=… pending=…` |
| 08:40 | `SCHED: tick prep options tracked=…` |
| 09:00 | `LIVETICKLOG: recording to /data/live_tick_logs/tsmc_ticks_YYYYMMDD.csv` |
| 09:00 | `SCHED: tick recorder started, snapshot rows=…` |
| 13:40 | `SCHED: eod replay YYYY-MM-DD launched (pid …)` |
| during replay | `EOD: N ticks, … scans, … LP screens, … full LP solves` (progress) |
| end | `EOD: YYYY-MM-DD: N ticks -> X Direct + Y LP episodes in …s` |
| end | `SCHED: eod replay YYYY-MM-DD exited 0` |

Then open the app: **Live Arb → EOD Replay** and pick the date.

During the session you can also check that recording works: **Live Arb → Direct Match** shows "recording — N rows so far" next to the button (which then reads "Stop Recording"; don't click it), and **Download CSV** gets what's been recorded so far.

---

## Starting mid-day (deployed after 08:40)

If the deploy finishes after 08:40 on a trading day, recording still starts automatically within 5 minutes (the `tick_record` job). But the 08:40 full-chain subscription was missed, so it would only record warrants already on the Live Warrant tracked list. To get the whole chain right away:

1. **Live Warrant tab:** under Liquidity Scan, choose underlying **2330**, set **Top N = 0** (the whole chain), and click **Run Scan**.
2. **Live Options tab:** click **Load TSMC Chain**. The `tick_record` job also does this by itself if the chain is empty.
3. Within 5 minutes, look for `SCHED: tick recorder started` in the logs. Newly subscribed instruments appear in the CSV as they seed or tick.

That day's replay still runs at 13:40 on whatever was recorded. From the next day on, the 08:40 job handles everything.

---

## Operating notes

- **Don't redeploy between 13:40 and the end of the replay** (it could take a few hours on the full chain). A redeploy kills the replay process. If that happens, open **EOD Replay**, pick the day, and click **Re-run**; the tick file is safe on the volume.
- **A redeploy during the session (09:00–13:30)** is fine: the recorder flushes on shutdown, and within 5 minutes of coming back it resumes the same file with a fresh snapshot. You only lose the ticks during the downtime.
- **The server must stay up** from 08:40 until the replay finishes.
- **Fubon login:** the recorder shares the same Fubon session as the Live Warrant/Options tabs (one account, one process), so there's no extra login.
- **Re-running a past day:** possible as long as its tick file is still on disk (90 days by default).
- **Turning it off:** set `ENABLE_TICK_RECORDING=0` and redeploy.

### Testing locally (optional)

```bash
conda activate warrants          # or the repo's .venv
pip install -r requirements.txt  # includes highspy

# Generate the two fake days and replay them without touching Supabase
python scripts/gen_fake_ticks.py --out-dir /tmp/fake
TZ=Asia/Taipei python scripts/eod_arb_replay.py --csv /tmp/fake/fake_noarb_tsmc_ticks_20261007.csv --dry-run
TZ=Asia/Taipei python scripts/eod_arb_replay.py --csv /tmp/fake/fake_arb_tsmc_ticks_20261007.csv --dry-run --out /tmp/fake/arb.json

# Full test suite, both engines
TZ=Asia/Taipei python -m pytest tests -q
RUST_ENGINE=python TZ=Asia/Taipei python -m pytest tests -q
```

On macOS the vendored Fubon wheel is Linux-only and fails to install. Install everything except that line (`grep -v fubon requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt`); it isn't needed for the replay or the tests.

---

## File map

**New**
- `logic/eod_arb_replay.py`: the replay engine and episode tracking
- `logic/lp_screen.py`: warm-started LP relaxation screen
- `services/db_eod_arb.py`: Supabase read/write for runs and episodes
- `scripts/eod_arb_replay.py`: replay CLI (run by the scheduler, the Re-run button, or by hand)
- `scripts/gen_fake_ticks.py`: synthetic tick-day generator
- `static/js/live_arb_eod.js`: EOD Replay subtab
- `supabase/migrations/027_eod_arb_episodes.sql`: new tables
- `tests/logic/test_eod_arb_replay.py`, `tests/services/test_live_tick_log.py`

**Changed**
- `services/live_tick_log.py`: buffered writes, configurable dir, gzip, retention, stale-quote drop
- `services/live_warrant.py`, `services/live_options.py`: shared tick-row builder; rest/terms/snapshot rows
- `services/scheduler.py`: the 4 new jobs
- `logic/live_arb_logic.py`, `logic/live_arb_lp_logic.py`: Python-engine hooks
- `app.py`: EOD routes
- `templates/index.html`, `static/js/live_arb.js`: subtab
- `wsgi.py`: flush the recorder on shutdown
- `requirements.txt`: `highspy`
- `supabase/schema.sql`, `CLAUDE.md`: docs
