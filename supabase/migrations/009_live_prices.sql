-- Migration 009: live_prices — the worker→web relay for live tick prices.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
--
-- broker_worker.py and app.py are separate Render services with no shared
-- memory, so a Tick (services/broker/base.py) can't reach the web process
-- directly. The worker upserts each tick here; the web process polls this
-- table (services/broker/live_price.py) into an in-process cache that the
-- SSE endpoint streams to browsers.
--
-- code is the primary key, not a surrogate id: this is a live CACHE, not a
-- tick history. One row per watched code, so the table stays the size of the
-- Watchlist no matter how many ticks flow through it.
create table if not exists live_prices (
  code text primary key,
  price double precision not null,
  -- The tick's own exchange timestamp, not the row's write time — no default now().
  ts timestamptz not null,
  broker text not null
);
alter table live_prices enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_prices to service_role;
