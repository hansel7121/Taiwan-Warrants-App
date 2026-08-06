-- Migration 013: live_price_ticks — append-only tick history (#52).
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
--
-- live_prices (migration 009) is a one-row-per-code cache, overwritten on
-- every print; this table keeps every print instead, for history/analysis.
-- Retention is enforced app-side (broker_worker._cleanup_tick_history, #52),
-- not by Postgres, so unbounded growth needs no separate migration to add.
create table if not exists live_price_ticks (
  id bigint generated always as identity primary key,
  code text not null,
  broker text not null,
  price double precision not null,
  qty integer,
  ts timestamptz not null,
  inserted_at timestamptz not null default now()
);
create index if not exists live_price_ticks_code_ts_idx
  on live_price_ticks (code, ts);
alter table live_price_ticks enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_price_ticks to service_role;
