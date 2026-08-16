---
status: accepted
---

# Live Warrant: single-process, single-account for v1; defer pooled multi-account/Redis architecture

Live Warrant v1 (a shared Fubon order-book feed, admin-only, driven by one broker account) runs inside the main Flask process — same in-memory-cache-plus-background-thread pattern the scheduler already uses for CMoney/TAIFEX/yfinance syncs — rather than as a separate `broker-connector` service behind Redis pub/sub and SSE.

**Why:** the app already runs exactly one gunicorn worker (market-data caches are process-memory, per `CLAUDE.md`), so there is no multi-replica fan-out problem today that would justify pulling broker-socket handling into its own process. Live-warrant v1 (pre-teardown, see `archive/live-warrant-v1`) grew to ~32k lines specifically because Render forced web and broker into separate services with no shared memory, producing relay tables, pollers, and a third SSE service to re-derive what one process gets for free. The Hetzner migration was chosen to undo exactly that split; building the pooled architecture immediately would reintroduce it on new infrastructure.

**Considered and not adopted:** a multi-service, multi-account pooled design built up front for eventual multi-user scale. Rejected for v1 as premature — it solves a concurrency problem (many web replicas needing one shared broker feed) that doesn't exist yet, at real cost (a message relay, cross-process failure handling). Not designed in detail; revisit from scratch if/when it's actually needed.

**Consequences:** the connection-pool logic (lazy-open, first-fit across a Fubon account's 7 connections, hard-reject at its subscription cap) must be written generically — a list of `(account, connection, subs_count)` — so that scaling from 1 account to N later is additive plumbing (new `broker_accounts` rows, an outer loop) rather than a rewrite. The trigger for revisiting this ADR is opening Live Warrant to non-admin users, and should be a measured decision (observed GIL contention between Fubon message-handling and request-serving threads under real trading-hour load), not a speculative one — see `docs/adr/` for the successor ADR once that happens.
