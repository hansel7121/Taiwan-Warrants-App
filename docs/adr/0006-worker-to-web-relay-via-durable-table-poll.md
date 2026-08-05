# Ticks reach the web process via a durable table + short poll, not Supabase Realtime

This concerns only the worker-to-web hop — ADR-0003 covers the separate web-to-browser hop (SSE), which is unchanged by this decision.

`broker_worker.py` and the Flask app are separate Render services with no shared memory, so a Tick landing in the worker's `ConnectionPool` callback can't reach the web process's in-memory cache directly. Supabase Realtime was the first option considered, since Supabase is already the shared system of record — but this codebase's `supabase-py` client is the sync client, and its `.channel()` raises `NotImplementedError`; Realtime needs the async client, and there is no asyncio anywhere else in this repo. Adopting it here would mean carrying a second Supabase client (or an async runtime) for one feature.

Instead, the worker upserts every tick into a `live_prices` table (one row per code, `code` as primary key), and the web process polls that table on a plain interval (`services/broker/live_price.py`, default 1s) into an in-process `TTLCache`. This makes the relay a durable, at-rest handoff rather than a live subscription: a web-process restart just re-reads the current row instead of losing ticks, and the poll is cheap enough (single-table `select *`) to run unconditionally alongside the gated scheduler jobs.
