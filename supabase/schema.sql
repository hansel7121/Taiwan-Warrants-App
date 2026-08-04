-- Supabase schema for Taiwan-Warrants-Web.
-- Run this ONCE in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Creates the allowed_users allow-list and per-user portfolio table, enables
-- Row Level Security, and adds policies so each authenticated user can only
-- read/write their own rows.
-- This file is the current end state. An existing deployment created from an
-- older copy is brought up to date by the files in migrations/ instead.

create table allowed_users (email text primary key, note text, created_at timestamptz default now());
-- portfolio.deleted_at is a tombstone (null = live). Two-way sync between the
-- Render and localhost instances replicates a delete as an update; a hard
-- delete would let one writer erase the other's unseen entries. updated_at is
-- the sync cursor and must never be null.
create table portfolio (
  id text not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  payload jsonb not null,
  deleted_at timestamptz,
  created_at timestamptz default now(), updated_at timestamptz not null default now(),
  primary key (user_id, id)
);
create index portfolio_user_updated_at_idx on portfolio (user_id, updated_at desc);
alter table portfolio enable row level security;
alter table allowed_users enable row level security;
create policy "own rows" on portfolio for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- SERVER-ONLY market-data tables (Supabase market-data migration, Phase 2).
--
-- These are written and read ONLY by the server using the service-role key,
-- which BYPASSES row level security. Following the allowed_users precedent
-- above, RLS is enabled with NO policy: that denies every anon/authenticated
-- client (the browser) while the service role still has full access. Never add
-- a policy here — a policy would expose the raw market snapshots to end users.
--
-- Snapshot model: each category (warrants / tw_options / us_options /
-- warrant_universe) is a set of rows tagged by batch_id. md_batches holds the
-- current-generation batch_id per category; a writer inserts a whole new batch,
-- flips the md_batches pointer, then deletes the old batch. supabase-py has no
-- transactions, so the pointer flip is the atomicity mechanism: a concurrent
-- reader always resolves one complete batch, never a half-written one.
-- ─────────────────────────────────────────────────────────────────────────────

-- Current-generation batch pointer, one row per category.
create table if not exists md_batches (
  category text primary key,
  batch_id uuid not null,
  created_at timestamptz not null default now()
);

-- Warrant snapshot rows (columns from logic/warrant_logic.py COL_ORDER).
create table if not exists md_warrants (
  batch_id uuid not null,
  warrant_code text,
  warrant_name text,
  underlying_code text,
  type text,
  underlying_price double precision,
  ask double precision,
  bid double precision,
  ask_qty double precision,
  bid_qty double precision,
  days_to_expiry integer,
  strike double precision,
  exercise_ratio double precision,
  volume double precision,
  time_value double precision,
  time_value_pct double precision,
  time_value_am double precision,
  iv_ask double precision,
  iv_bid double precision,
  delta_calc double precision,
  leverage_calc double precision
);
create index if not exists md_warrants_batch_code_idx on md_warrants (batch_id, underlying_code);

-- Taiwan option snapshot rows: UNION of the MIS intraday row (fetch_options_mis)
-- and the EOD row (_parse_and_compute) in logic/options_logic.py. EOD rows lack
-- bid_size / ask_size / quote_time, so those are nullable. strike is numeric
-- (not integer): MIS single-stock-option strikes can be fractional.
create table if not exists md_tw_options (
  batch_id uuid not null,
  stock_code text,
  source text,
  contract text,
  type text,
  underlying_price double precision,
  ask double precision,
  bid double precision,
  days_to_expiry integer,
  strike numeric,
  exercise_ratio double precision,
  bid_size integer,
  ask_size integer,
  volume integer,
  oi integer,
  time_value_am double precision,
  iv_ask double precision,
  iv_bid double precision,
  delta_calc double precision,
  leverage_calc double precision,
  is_live boolean,
  ask_live boolean,
  bid_live boolean,
  quote_time text
);
create index if not exists md_tw_options_batch_stock_idx on md_tw_options (batch_id, stock_code);

-- US ADR option snapshot rows (columns from logic/us_options_logic.py
-- fetch_us_options built row). No leverage_calc on this leg.
create table if not exists md_us_options (
  batch_id uuid not null,
  stock_code text,
  contract text,
  type text,
  underlying_price double precision,
  strike double precision,
  days_to_expiry integer,
  bid double precision,
  ask double precision,
  iv_ask double precision,
  iv_bid double precision,
  delta_calc double precision,
  volume integer,
  oi integer,
  is_live boolean,
  ask_live boolean,
  bid_live boolean,
  strike_usd double precision,
  bid_usd double precision,
  ask_usd double precision,
  adr_price double precision,
  fx double precision
);
create index if not exists md_us_options_batch_stock_idx on md_us_options (batch_id, stock_code);

-- Daily listed-warrant universe snapshot (written by the daily universe job).
create table if not exists md_warrant_universe (
  batch_id uuid not null,
  code text,
  name text,
  start date,
  market text,
  type text
);
create index if not exists md_warrant_universe_batch_code_idx on md_warrant_universe (batch_id, code);

-- Single-row CMoney API token store (survives restarts). NOT a snapshot
-- category — a plain one-row upsert keyed on id=1.
create table if not exists cmoney_key (
  id integer primary key default 1,
  key text,
  updated_at timestamptz
);

-- Tracked-product lists (Phase 5 of the naming/correctness plan). Shared
-- across all users — no per-user scoping, no is_admin gate. Same RLS pattern
-- as the md_* tables above: enabled, no policy, service-role only.
create table if not exists warrant_stocks (
  code text primary key,
  name text,
  created_at timestamptz default now()
);

create table if not exists tw_option_products (
  code text primary key,
  commodity_ids text[] not null,
  ticker text not null,
  exercise_ratio int not null,
  name text,
  created_at timestamptz default now()
);

create table if not exists us_option_products (
  code text primary key,
  adr_ticker text not null,
  fx_ticker text not null,
  adr_ratio numeric not null,
  name text,
  created_at timestamptz default now()
);

-- Automated Direct-Arb scanner output (scheduler.py:sync_suggestions), same
-- server-only RLS pattern as the tables above. id is a deterministic string
-- built from (arb_type, sorted leg codes, strikes, expiry/contract,
-- direction) so re-finding the same opportunity next cycle upserts the same
-- row instead of creating a duplicate. status has no 'dismissed' value —
-- user-initiated removal is a hard delete (services/db_suggestions.py),
-- since the next scan cycle would just re-upsert the same id anyway.
create table if not exists arb_suggestions (
  id text primary key,
  arb_type text not null,
  legs jsonb not null,
  price_diff numeric not null,
  price_diff_pct numeric,
  legs_status jsonb not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  status text not null default 'active',
  created_at timestamptz default now()
);
create index if not exists arb_suggestions_status_idx on arb_suggestions (status, last_seen_at desc);

alter table warrant_stocks enable row level security;
alter table tw_option_products enable row level security;
alter table us_option_products enable row level security;
alter table arb_suggestions enable row level security;

grant all on warrant_stocks, tw_option_products, us_option_products, arb_suggestions to service_role;

alter table md_batches enable row level security;
alter table md_warrants enable row level security;
alter table md_tw_options enable row level security;
alter table md_us_options enable row level security;
alter table md_warrant_universe enable row level security;
alter table cmoney_key enable row level security;

-- The service role bypasses RLS but still needs table-level privileges. Tables
-- created via the SQL editor do not always inherit the default grants, so grant
-- explicitly (only to service_role — anon/authenticated stay blocked by RLS).
grant all on md_batches, md_warrants, md_tw_options, md_us_options, md_warrant_universe, cmoney_key to service_role;

-- ─────────────────────────────────────────────────────────────────────────────
-- Broker credentials (KGI / Fubon) — per-user, encrypted at rest.
--
-- The Singapore worker logs in unattended before market open every trading day,
-- so credentials must persist rather than be re-entered (docs/adr/0002).
-- encrypted_fields is a Fernet token over the credential dict, keyed by the
-- BROKER_CRED_KEY env var: the database never sees plaintext, so a database
-- leak alone is not enough to log in as the user.
--
-- Also required, ONCE, by hand in the Supabase dashboard (Storage -> New
-- bucket): a bucket named "broker-certs", PRIVATE (not public), holding KGI's
-- manually generated cert and later Fubon's .pfx at {user_id}/{broker}/cert.ext.
-- A private bucket needs no storage policies: the server uses the service-role
-- key, which bypasses Storage RLS the same way it bypasses table RLS, and a
-- public bucket would serve certs to anyone with the URL.
create table if not exists broker_credentials (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  encrypted_fields text not null,
  -- Capacity tier, populated for every row whatever the broker, defaulted
  -- per-broker on write (kgi 30/2, fubon 200/5). KGI's tier varies by account
  -- and is entered by hand (the API exposes it nowhere), so its default is only
  -- a starting point a caller can override; Fubon's is fixed for every account
  -- and is mirrored here purely so readers get one uniform shape.
  symbols_per_connection int,
  connections int,
  cert_path text,
  created_at timestamptz default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists broker_credentials_user_idx on broker_credentials (user_id);
alter table broker_credentials enable row level security;
-- Per-user table, so it follows portfolio's "own rows" policy rather than the
-- md_* tables' service-role-only grant: defense in depth, since the server
-- itself reaches this table with the service-role key, which bypasses RLS.
create policy "own rows" on broker_credentials for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Broker connect/disconnect control plane.
--
-- The UI and the Singapore worker are separate processes that never call each
-- other — the browser cannot reach the worker's box, and the worker is offline
-- whenever the user clicks. So the two halves communicate through these two
-- tables instead: the user writes what they *want* (broker_desired_state), the
-- worker writes what *is* (worker_status), and each side polls the other.
--
-- broker_desired_state is deliberately not a queue: the worker only ever needs
-- the newest intent, and a click-happy user toggling connect/disconnect five
-- times should not make the worker replay five transitions. One row per
-- (user_id, broker), upserted in place — latest wins.
create table if not exists broker_desired_state (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  desired_state text not null check (desired_state in ('connect', 'disconnect')),
  requested_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists broker_desired_state_user_idx on broker_desired_state (user_id);
alter table broker_desired_state enable row level security;
-- Per-user table, so it follows broker_credentials' "own rows" policy: defense
-- in depth, since the server itself reaches this table with the service-role
-- key, which bypasses RLS.
create policy "own rows" on broker_desired_state for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- worker_status is the worker's side of the conversation: one row per broker
-- account, overwritten on every transition. Server-only (RLS on, no policy,
-- like the md_* tables) because a user's own client must not be able to forge a
-- "connected" row — the status is only meaningful if the worker is its sole
-- author. The browser reads it through /broker/status, which is server-side and
-- scopes the query to the caller.
create table if not exists worker_status (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  status text not null check (status in ('connected', 'stopped', 'reconnecting', 'disconnected')),
  changed_at timestamptz not null default now(),
  unique (user_id, broker)
);
create index if not exists worker_status_user_idx on worker_status (user_id);
alter table worker_status enable row level security;
grant all on worker_status to service_role;

-- The service role bypasses RLS but still needs table-level privileges, and a
-- table created through the SQL editor does not always inherit the default
-- grants. broker_credentials and broker_desired_state both shipped without an
-- explicit grant and came out unreadable with the service-role key, which is
-- the only key the worker has.
grant all on broker_credentials, broker_desired_state to service_role;

-- ─────────────────────────────────────────────────────────────────────────────
-- Live arb signals — the Singapore worker's output.
--
-- One row per arb found while checking a single broker Tick against the
-- worker's option-side mirror. Deliberately separate from arb_suggestions, with
-- no merge or dedup between them (docs/adr/0007), because the lifecycles
-- differ: a suggestion is scan-to-scan state that can go stale, so it carries
-- status/first_seen_at/last_seen_at; a signal is an event whose meaning is tied
-- to the instant of the Tick that produced it. Hence no status column and
-- nothing to flip — rows are appended, read newest-first, and aged out by time.
-- Identity is a surrogate uuid rather than arb_suggestions' deterministic
-- composite id: the same warrant/option pair on the next Tick is a new
-- observation, not the same row seen again.
--
-- Raw Ticks are still never persisted (docs/adr/0003): tick_ts/tick_broker say
-- which print a signal was derived from, not the print itself.
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
-- Server-only like md_* and worker_status: RLS on with NO policy, so only the
-- service-role key reaches it. A signal is the worker's assertion; a browser
-- must not be able to write one.
alter table arb_signals enable row level security;
grant all on arb_signals to service_role;
