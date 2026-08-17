# Booting all 7 Fubon connections

Step-by-step guide for opening all seven Fubon websocket connections
(2,100 subscriptions total) using `scripts/fubon_quote_viewer.py`, with
credentials pulled from Supabase — no local `.env` broker secrets required.

## Before you start

- Repo cloned, checked out to `ian/fubon-viewer-connections-perf`
- Conda env `warrants` created (`pip install -r requirements.txt`)
- `fubon_neo` SDK importable in that env
- `.env` at repo root with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `BROKER_CRED_KEY` — ask Ian for these three values
- Nothing else — the Fubon login itself now lives encrypted in Supabase,
  not in your `.env`

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

## Step 2: Pick a ranking mode

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

## Step 3: Launch connection 0

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

## Step 4: Launch connections 1–6

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

## Step 5: Verify all seven hit the cap cleanly

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

---

Branch `ian/fubon-viewer-connections-perf` · credentials come from the
`fubon_credentials` table via `services/broker/` (Fernet-encrypted, same
key as before) · script is `scripts/fubon_quote_viewer.py`
