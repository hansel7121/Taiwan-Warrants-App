# Broker Live Scanner

Context for the live-broker-connected warrant scanner: subscribes to real-time warrant price ticks via a broker's market-data API (KGI, Fubon) and scans them for arbitrage against the existing periodic option-side data.

## Language

**Watchlist**:
The single shared list of warrant codes subscribed to for live ticks, editable by any user, whose codes get split across whichever Broker Accounts have room. Persisted so the daily scheduled login can resubscribe automatically.
_Avoid_: Tracked stock, universe — those refer to `warrant_stocks`, the underlying-level list driving the periodic scanner, a different concept. Also avoid implying it's per-user — it is one shared list, not "my watchlist" / "his watchlist".

**Broker Account**:
One user's credentials for one broker (e.g. the KGI login belonging to one user, the Fubon login belonging to another). Each contributes its own Capacity Tier toward the shared Connection Pool. Self-service: a user only ever manages their own Broker Account, never another's.
_Avoid_: Broker credentials (that's the encrypted data itself; a Broker Account is the higher-level "whose login is this, on which broker" concept)

**Tick**:
A single live price update for one warrant code, pushed by the broker over its market-data connection. Exists only in worker process memory — never persisted or exposed outside the worker.
_Avoid_: Quote, price update

**Option Mirror**:
The worker's own in-memory copy of option-side data, built by polling the `md_*` batch-pointer tables in Supabase on an interval matching that data's real refresh cadence, and compared against incoming Ticks to produce Arb Signals. The worker keeps its own (one worker serves both brokers, so there is one shared Mirror, not one per broker connection); it is a read of what the web app already writes, never a separate fetch from TAIFEX/yfinance.
_Avoid_: Cache, snapshot — "snapshot"/"batch" already mean the `md_*` rows themselves; the Mirror is the worker's local view of them.

**Arb Signal**:
A detected arbitrage opportunity produced by comparing a live Tick against a cached mirror of option-side data, via one of the registered strategy checks. The only broker-tick-derived data written to the database.
_Avoid_: Result, opportunity — reserve "opportunity" language for the existing periodic `arb_suggestions`; this is the live-detected counterpart.

**Capacity Tier**:
A broker account's limits on live subscriptions — symbols per connection and connections per account. KGI's tier varies by account and is manually configured; Fubon's is fixed regardless of account.
_Avoid_: Plan, limit

**Connection Pool**:
Worker-side logic that spreads the shared Watchlist's codes across every connected Broker Account's connections, combining their Capacity Tiers into one pool, opening new connections (or overflowing to the other broker) as each fills.

**Worker Status**:
Persisted connection/health state per Broker Account — one of `connected`, `stopped` (expected: scheduled end-of-day close or a user's own manual disconnect, never alerts), `reconnecting` (retrying within the backoff window after an unexpected drop), or `disconnected` (terminal: backoff exhausted, alerts) — plus a last-tick timestamp, that the web UI reads for a live status indicator (both a combined summary and a per-account breakdown), and that other consumers (a notification channel, future monitoring) can read independently.
_Avoid_: Health check, heartbeat
