-- Migration 012: live_depth — 5-level bid/ask relay table (#51).
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly.
create table if not exists live_depth (
  code text primary key,
  bid_prices double precision[] not null,
  bid_volumes integer[] not null,
  ask_prices double precision[] not null,
  ask_volumes integer[] not null,
  ts timestamptz not null,
  broker text not null
);
alter table live_depth enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_depth to service_role;
