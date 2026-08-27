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
  bid_time_value_pct double precision,
  ask_time_value_pct double precision,
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

-- Encrypted Fubon login storage (scripts/fubon_quote_viewer.py), keyed by
-- label so more than one account can be stored without a schema change.
-- encrypted_fields is a Fernet token over {fubon_id, fubon_password,
-- cert_password}, keyed by the BROKER_CRED_KEY env var — see migration 022.
-- Same server-only RLS pattern as md_* / cmoney_key above. The .p12 cert
-- itself lives in the private "broker-certs" Storage bucket at
-- fubon/{label}/cert.p12 (bucket created by hand in the dashboard).
create table if not exists fubon_credentials (
  label text primary key,
  encrypted_fields text not null,
  cert_path text,
  created_at timestamptz default now(),
  updated_at timestamptz not null default now()
);
alter table fubon_credentials enable row level security;
grant all on fubon_credentials to service_role;

-- Live Warrant tab's shared tracked-code list (migration 023). Same
-- server-only RLS pattern as warrant_stocks/arb_suggestions. source
-- distinguishes a Liquidity Scan row (replaced on the next scan of the same
-- underlying) from a manually-added row (never touched by any scan); see
-- logic/live_warrant_logic.py::scan_replace.
create table if not exists live_warrant_tracked (
  code text primary key,
  name text,
  source text not null check (source in ('scan', 'manual')),
  underlying text,
  created_at timestamptz not null default now(),
  -- Contract terms (migration 024): fetched once per code and never change,
  -- so persisting them is what lets a restart skip the REST round trip
  -- entirely instead of re-fetching every tracked code. terms_fetched_at
  -- marks "looked up, whatever came back" — the value columns alone can't
  -- distinguish that from "never looked up" since any of them can come back
  -- null on a malformed payload and still count as done.
  strike double precision,
  exercise_ratio double precision,
  maturity date,
  terms_fetched_at timestamptz,
  constraint live_warrant_tracked_underlying_only_for_scan
    check (source = 'scan' or underlying is null)
);
create index if not exists live_warrant_tracked_underlying_idx
  on live_warrant_tracked (underlying) where source = 'scan';
alter table live_warrant_tracked enable row level security;
grant all on live_warrant_tracked to service_role;

-- ─────────────────────────────────────────────────────────────────────────────
-- USER-MODE dashboard tables (migration 005_user_dashboard.sql).
-- Per-user and RLS-scoped like `portfolio`, NOT shared like the product tables.
-- ─────────────────────────────────────────────────────────────────────────────
-- Starred instruments. `kind` decides how `code` is interpreted: a warrant code
-- ("031100") or a TAIFEX contract label ("C600 15Aug26"). meta snapshots the
-- descriptive fields at star time (type/strike/expiry/name) so the dashboard can
-- render a row before any quote comes back.
create table if not exists user_watchlist (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('warrant', 'tw_option')),
  code text not null,
  underlying_code text,
  label text,
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (user_id, kind, code)
);
create index if not exists user_watchlist_user_idx on user_watchlist (user_id, created_at desc);

-- Threshold alerts on a starred instrument. Evaluated in the browser when the
-- dashboard loads (see static/js/dashboard.js); last_triggered_at / last_value
-- are written back so a fire is still visible on the next load.
create table if not exists user_alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('warrant', 'tw_option')),
  code text not null,
  underlying_code text,
  metric text not null check (metric in ('bid', 'ask', 'iv', 'underlying')),
  direction text not null check (direction in ('above', 'below')),
  threshold double precision not null,
  note text,
  active boolean not null default true,
  last_triggered_at timestamptz,
  last_value double precision,
  created_at timestamptz not null default now()
);
create index if not exists user_alerts_user_idx on user_alerts (user_id, created_at desc);

-- A position is a container; the legs carry the instruments. One-to-many, so a
-- position can be any number of warrant / option / underlying legs.
create table if not exists user_positions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text,
  underlying_code text,
  note text,
  opened_at timestamptz not null default now(),
  closed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists user_positions_user_idx on user_positions (user_id, opened_at desc);

-- quantity units depend on kind, matching how each instrument actually trades:
--   warrant     -> board lots (張); 1 lot = 1,000 units, each unit delivering
--                  exercise_ratio underlying shares
--   tw_option   -> contracts; contract_size (2,000) shares each
--   underlying  -> shares
-- entry_price is always per the instrument's own quoted unit: NT per warrant
-- unit, NT per option point/share, NT per share.
-- user_id is denormalized from the parent so the RLS policy is a plain
-- column check with no subquery.
create table if not exists user_position_legs (
  id uuid primary key default gen_random_uuid(),
  position_id uuid not null references user_positions(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('warrant', 'tw_option', 'underlying')),
  code text not null,
  label text,
  direction smallint not null check (direction in (-1, 1)),
  quantity double precision not null,
  entry_price double precision not null,
  option_type text check (option_type in ('Call', 'Put')),
  strike double precision,
  days_to_expiry integer,
  exercise_ratio double precision,
  contract_size double precision,
  iv double precision,
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists user_position_legs_position_idx on user_position_legs (position_id);
create index if not exists user_position_legs_user_idx on user_position_legs (user_id);

alter table user_watchlist enable row level security;
alter table user_alerts enable row level security;
alter table user_positions enable row level security;
alter table user_position_legs enable row level security;

create policy "own rows" on user_watchlist for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own rows" on user_alerts for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own rows" on user_positions for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own rows" on user_position_legs for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- The service role bypasses RLS but still needs table-level privileges.
grant all on user_watchlist, user_alerts, user_positions, user_position_legs to service_role;
