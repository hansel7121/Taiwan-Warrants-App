# Fubon quote viewer

`scripts/fubon_quote_viewer.py` is a standalone Fubon live order-book
viewer — not part of the main app. It logs in once, opens a websocket,
subscribes a handful of codes, and serves a page showing each one's live
five-level bid/ask ladder. Credentials come from Supabase (`fubon_credentials`
table, Fernet-encrypted — see `services/broker/`), not from local `.env`
broker secrets.

## Before you start

- Conda env `warrants` created (`pip install -r requirements.txt`)
- `fubon_neo` SDK importable in that env
- `.env` at repo root with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `BROKER_CRED_KEY` — ask Ian for these three values
- Nothing else — the Fubon login itself lives encrypted in Supabase, not in
  your `.env`. If no `fubon_credentials` row exists yet, run
  `python scripts/seed_fubon_credentials.py` once to populate one from a
  local `.env` (`FUBON_ID`, `FUBON_PASSWORD`, `FUBON_CERT_PATH`,
  `FUBON_CERT_PASSWORD`).

## Step 1: Confirm the credential row is reachable

This should print one row. If it prints nothing or errors, your three
Supabase env vars are wrong — fix that before going further.

```bash
python -c "
import sys; sys.path.insert(0, '.')
from services.broker import credentials
print(credentials.list_labels())
"
```

## Basic usage (one connection, no stress test)

Just run it — defaults watch warrant `2330`'s busiest 5 codes by traded
volume:

```bash
TZ=Asia/Taipei python scripts/fubon_quote_viewer.py
```

Open `http://127.0.0.1:5099`. Useful overrides:

| Var | Default | Meaning |
|---|---|---|
| `FUBON_VIEWER_PORT` | `5099` | Flask port (change to run a second instance alongside this one) |
| `FUBON_VIEWER_MARKET` | `stock` | `stock` watches warrants; `futopt` watches TAIFEX options instead |
| `FUBON_VIEWER_UNDERLYING` | `2330` | Underlying stock code to rank warrants for (`stock` mode only) |
| `FUBON_VIEWER_TOP_N` | `5` | How many busiest warrant codes to auto-subscribe |
| `FUBON_VIEWER_OPTION_PRODUCT` | `TXO` | TAIFEX option product to watch (`futopt` mode only) |
| `FUBON_VIEWER_OPTION_TOP_N` | `5` | How many contracts nearest the money to auto-subscribe (`futopt` mode) |
| `FUBON_VIEWER_OPTION_STRIKE_OFFSET` | `0` | Skip this many strikes before picking `OPTION_TOP_N`, for running several `futopt` instances on disjoint strike ranges |
| `FUBON_CRED_LABEL` | `ian` | Which `fubon_credentials` row to log in with |

Options example:

```bash
TZ=Asia/Taipei FUBON_VIEWER_MARKET=futopt FUBON_VIEWER_OPTION_PRODUCT=TXO \
python scripts/fubon_quote_viewer.py
```

Note: the book only streams during market hours (TWSE 09:00-13:30 TPE for
`stock` mode, weekdays). Outside them the socket still connects and
subscribes, and the page says so, but no levels arrive.

## Stress test: booting all 7 connections at once

The rest of this doc is a step-by-step guide for opening all seven Fubon
websocket connections (2,100 subscriptions total) at once, to confirm the
account-wide subscription cap — not needed for everyday use above.

### Step 2: Pick a ranking mode

Both modes subscribe the same 2,100 codes across the twelve large-cap
underlyings with the deepest warrant chains — they only differ in *which*
300 per connection and in what order.

- **`FUBON_VIEWER_STRESS_MODE=liquid`** — ranks the pool by today's traded
  volume via TWSE MIS, so every connection's slice actually has warrants
  people are trading, not dead ones nobody quotes. This is the whole reason
  to run the test from Taiwan: MIS is blocked from Ian's US IP, so this
  mode has never actually been exercised yet.
- **`FUBON_VIEWER_STRESS_MODE=code`** — skips MIS entirely and sorts the
  pool by warrant code. No dependency on MIS being reachable; useful as a
  control, or if MIS turns out to be blocked from your IP too.

Run both, back to back — that's the actual point of testing from Taiwan.

| Mode | Pros | Cons |
|---|---|---|
| `liquid` | Proves MIS ranking (broken from Ian's US IP); books fill in fast since codes are actively traded | Needs MIS reachable — the thing under test |
| `code` | Zero dependency on MIS — pure connection/subscription test | Some books stay quiet if that code isn't actively quoted |

### Step 3: Launch connection 0

This one does the extra work: it lists warrants for all twelve
underlyings, ranks them (mode-dependent), and writes the shared cache file
the other six connections read from. Run it first and wait for it to
settle before starting the rest.

```bash
TZ=Asia/Taipei \
FUBON_VIEWER_PORT=5099 \
FUBON_VIEWER_STRESS_TARGET=300 \
FUBON_VIEWER_STRESS_OFFSET=0 \
FUBON_VIEWER_STRESS_MODE=liquid \
FUBON_VIEWER_STRESS_RANK_CACHE=/tmp/fubon_rank_liquid.json \
python scripts/fubon_quote_viewer.py
```

(swap `FUBON_VIEWER_STRESS_MODE=liquid` for `code` when testing the other
mode)

> **Cache filename matters.** Use a different
> `FUBON_VIEWER_STRESS_RANK_CACHE` path per mode (e.g.
> `fubon_rank_liquid.json` vs `fubon_rank_code.json`). If you reuse the
> same file across a mode switch, every connection just reads the first
> ranking it ever computed, regardless of the mode you set the second
> time.

Open `http://127.0.0.1:5099` and watch the status line. Wait until it
stops saying "ranking…" / "stress test: finding warrants…" before moving
on — that's the cache being written.

### Step 4: Launch connections 1–6

Same command shape, one per port, each claiming the next disjoint
300-code slice from the cache connection 0 just built. These start
instantly since they just read the cache file rather than re-scanning
MIS. Fill in the same `FUBON_VIEWER_STRESS_MODE` and
`FUBON_VIEWER_STRESS_RANK_CACHE` path you used for connection 0.

| Terminal | PORT | OFFSET |
|---|---|---|
| 2 | 5100 | 300 |
| 3 | 5101 | 600 |
| 4 | 5102 | 900 |
| 5 | 5103 | 1200 |
| 6 | 5104 | 1500 |
| 7 | 5105 | 1800 |

```bash
TZ=Asia/Taipei \
FUBON_VIEWER_PORT=5100 \
FUBON_VIEWER_STRESS_TARGET=300 \
FUBON_VIEWER_STRESS_OFFSET=300 \
FUBON_VIEWER_STRESS_MODE=liquid \
FUBON_VIEWER_STRESS_RANK_CACHE=/tmp/fubon_rank_liquid.json \
python scripts/fubon_quote_viewer.py
```

Repeat for terminals 3–7 with the matching `PORT` / `OFFSET` from the
table above.

### Step 5: Verify all seven hit the cap cleanly

Each page's status line should settle at `subscriptions 300/300` with no
error. None should ever show the Fugle `1001: Subscription limit
exceeded` error — if one does, that connection tried to exceed the
measured per-connection cap.

```bash
curl -s http://127.0.0.1:5099/data | python -c "
import json,sys
d=json.load(sys.stdin)
print('subs=',d['subs'],'confirmed=',d['subs_confirmed'],'error=',d['error'])
"
```

Repeat for `5100` through `5105`. Seven connections × 300 confirmed, zero
errors, confirms the account-wide 2,100-subscription cap.

## Cross-market login cap: mixing stock + futopt connections

The 7-connection cap above was only ever measured on the `stock` side
alone. Opening `stock` and `futopt` connections *together* on one account
hits a lower, shared login-level cap — this isn't a subscription-cap error
(`1001` from Fugle), it's the broker rejecting the login itself:

```
login failed: Login Error, 超過本應用程式連線限制==>[10]
```

Measured by launching 7 `stock` + 7 `futopt` instances (`FUBON_CRED_LABEL`
pinned to one account, ports 5099-5105 for `stock` and 5106-5112 for
`futopt`, `FUBON_VIEWER_STRESS_MODE=code` to skip MIS):

| Run | Launched first | Launched second | Result |
|---|---|---|---|
| 1 | 7 `stock` (all 7 connected) | 7 `futopt` (5/7 connected, 2 failed) | 12/14 total |
| 2 | 7 `futopt` (all 7 connected) | 7 `stock` (6/7 connected, 1 failed) | 13/14 total |
| 3 | alternated `stock`/`futopt` one at a time, 3s between each launch | — | 13/14 total: all 7 `stock` + 6/7 `futopt`, only the 14th (final) login failed |

Run 3 shows the 3-second gap doesn't change the outcome in kind — logins
still land in the same ~12-13 range, and it's still whichever connection
happens to be *last* that fails, not a fixed market or slot. A slower,
one-at-a-time launch just makes the race deterministic (last login loses)
instead of letting several logins contend for the last slot at once.

Takeaways:

- The cap is **account-wide across markets, not per-market**. Whichever
  batch is opened *second* is the one that eats the failures — the market
  type doesn't matter, only login order.
- The real ceiling sits at roughly **12-13 concurrent logins**, not a
  clean 7-per-market split. The exact number (12 vs 13) and which
  connections fail varies run to run — logins fired in the same batch
  race each other at the broker, so the split isn't deterministic.
- **Implication for the connection-pool code** (`logic/live_warrant_logic.py`,
  `MAX_CONNECTIONS = 7`): that constant is a *per-market* subscription-slot
  budget, not a validated total login ceiling. If a future account/service
  ever opens `stock` and `futopt` connections concurrently under one
  broker login, 7 + 7 is not safe to assume — expect the second market's
  last connection or two to fail login instead of connecting.

To reproduce: repeat the launch sequence above (7 of one market, then 7 of
the other, `FUBON_VIEWER_STRESS_MODE=code`/`FUBON_VIEWER_OPTION_STRIKE_OFFSET`
staggered per instance so codes don't overlap), and poll `/data` on every
port — `connected: false` with the `超過本應用程式連線限制` error marks a
login-cap failure, distinct from the `1001` subscription-cap error above.
Prefer a non-`default` credential label for this test if the `default`
account has a live session in use elsewhere (e.g. production Live
Warrant), so the stress test can't disrupt it.

---

Credentials come from the `fubon_credentials` table via `services/broker/`
(Fernet-encrypted) · script is `scripts/fubon_quote_viewer.py`
