-- Migration 010: live_assignment — the worker's actual code->connection placement.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
--
-- Same process boundary as live_prices (docs/adr/0006): broker_worker.py and
-- app.py are separate Render services, so the web process cannot see the
-- worker's in-memory assignment. It used to recompute one with pool.assign(),
-- which is wrong — every intraday Watchlist edit goes through pool.reassign(),
-- and reassign deliberately keeps surviving codes on the connection they are
-- already streaming on rather than repacking. A fresh assign() therefore drifts
-- from reality after the first edit, and is_live ends up reading the wrong
-- account's Worker Status.
--
-- The worker publishes what it actually holds here, one row per code (the
-- reader only needs "which connection carries this code"), replaced wholesale
-- after every apply_watchlist().
create table if not exists live_assignment (
  code text primary key,
  broker text not null,
  user_id uuid not null,
  connection_index integer not null
);
alter table live_assignment enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_assignment to service_role;
