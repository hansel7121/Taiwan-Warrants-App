-- Migration 007: arb_signals + the service_role grants 005/006 forgot.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- arb_signals is the live worker's output: one row per arb the worker found
-- while checking a single broker Tick against its option-side mirror. It stays
-- fully separate from arb_suggestions (docs/adr/0007) — nothing merges or
-- dedups the two — because the lifecycles genuinely differ. A suggestion is
-- scan-to-scan state (it can go stale when the next scan no longer finds it, so
-- it carries status/first_seen_at/last_seen_at); a signal is an event, true only
-- at the instant of the Tick that produced it. So there is no status column and
-- nothing to flip: rows are appended, read newest-first, and aged out by time.
-- Identity is therefore a surrogate uuid, not the deterministic composite id
-- arb_suggestions uses — the same warrant/option pair reappearing on the next
-- Tick is a NEW observation, not the same row seen again.
--
-- Raw Ticks are still never persisted (docs/adr/0003): tick_ts/tick_broker
-- record which print a signal was derived from, not the print itself.
create table if not exists arb_signals (
  id uuid primary key default gen_random_uuid(),
  strategy text not null,
  underlying_code text,
  warrant_code text,
  option_contract text,
  legs jsonb not null,
  price_diff numeric,
  price_diff_pct numeric,
  tick_ts timestamptz,
  tick_broker text,
  detected_at timestamptz not null default now()
);
create index if not exists arb_signals_detected_idx on arb_signals (detected_at desc);
create index if not exists arb_signals_underlying_idx on arb_signals (underlying_code, detected_at desc);
-- Server-only, like md_* and worker_status: RLS on with NO policy, so only the
-- service-role key reaches it. A signal is the worker's assertion; a browser
-- must not be able to write one.
alter table arb_signals enable row level security;
grant all on arb_signals to service_role;

-- The service role bypasses RLS but still needs table-level privileges, and
-- tables created through the SQL editor do not always inherit the default
-- grants. Migrations 005 and 006 relied on that inheritance and both tables
-- came out unreadable with the service-role key ("permission denied for table
-- broker_desired_state"), which is exactly the query the worker's reconciliation
-- poll makes. Granting is idempotent, so this is safe to re-run.
grant all on broker_credentials, broker_desired_state to service_role;
