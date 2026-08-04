# Taiwan Warrants App

A local/hosted Flask app for scanning Taiwan stock warrants and equity options, computing implied volatility, and surfacing arbitrage between warrants and TAIFEX/US options.

## Language

**Broker Account**:
One user's credentials for one broker (e.g. the KGI login belonging to one user, the Fubon login belonging to another). Each contributes its own Capacity Tier toward the shared Connection Pool. Self-service: a user only ever manages their own Broker Account, never another's. One credential per user per broker for now (`unique(user_id, broker)`); multiple credentials per user per broker is a deferred future need, not built.
_Avoid_: Broker credentials (that's the encrypted data itself; a Broker Account is the higher-level "whose login is this, on which broker" concept)

**Capacity Tier**:
A broker account's limits on live subscriptions — symbols per connection and connections per account. KGI's tier varies by account and is manually configured; Fubon's is fixed regardless of account.
_Avoid_: Plan, limit

**Connection Pool**:
Worker-side logic that spreads the shared Watchlist's codes across every connected Broker Account's connections, combining their Capacity Tiers into one pool, opening new connections (or overflowing to the other broker) as each fills. One shared pool serves every consumer of the tick stream (currently the Live-price cache; later, live arb detection too) — not one pool per feature.

**Watchlist**:
The shared, freely-editable list of warrant codes the Connection Pool subscribes to live. Any user adds or removes codes explicitly (mirroring the existing tracked-product lists like `warrant_stocks`); a code stays on the list until someone removes it — no per-session auto-expiry, since another user may still want it watched after the first closes their tab. Edits take effect intraday (see `docs/adr/0002`) — the worker polls the Watchlist on a tight cadence during market hours and incrementally subscribes/unsubscribes changed codes on already-open connections, without disturbing unrelated subscriptions. If every connection is already at capacity, an added code is rejected outright (surfaced in the UI) rather than evicting an existing watch.

**Live-price cache**:
The in-worker store that the Connection Pool's ticks are written into for the Live-warrant sub-tab's use — the tick consumer for this feature, standing in where live arb detection (`arb_logic.check_tick()`) will later be added as a second consumer of the same tick stream.
_Avoid_: Live ticks, tick store (too generic — this is specifically the display-facing cache, not a persisted or shared table)
