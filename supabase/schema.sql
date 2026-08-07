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
-- Per-user broker credentials (Live warrant sub-tab; migration 005).
--
-- Each user stores their own KGI / Fubon login through the app. Secrets live
-- ONLY in encrypted_fields, a Fernet token over the credential dict keyed by
-- the BROKER_CRED_KEY env var (services/broker/crypto.py) — the database never
-- sees plaintext. One credential per user per broker for now (docs/adr/0004).
--
-- Requires, ONCE, by hand in the dashboard (Storage -> New bucket): a PRIVATE
-- bucket named "broker-certs" holding Fubon's .pfx at
-- {user_id}/{broker}/cert.pfx. KGI needs no cert here (manual one-time CLI step
-- outside the app).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists broker_credentials (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  broker text not null check (broker in ('kgi', 'fubon')),
  encrypted_fields text not null,
  -- Capacity tier, defaulted per-broker on write (kgi 30/2, fubon 200/5). KGI's
  -- varies by account and is entered by hand; Fubon's is fixed for every
  -- account and mirrored here so readers get one uniform shape.
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
-- reaches this table with the service-role key, which bypasses RLS.
create policy "own rows" on broker_credentials for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
grant all on broker_credentials to service_role;

-- Also requires, ONCE, by hand in the dashboard (Storage -> New bucket): a
-- PRIVATE bucket named "broker-binaries" holding KGI's libCGCrypt.so and
-- Fubon's fubon_neo wheel (uploaded under its own filename, not renamed —
-- see migration 015), fetched into the broker-worker container at boot
-- (worker-entrypoint.sh / scripts/install_worker_binaries.py). These are
-- proprietary compiled binaries this public repo can't commit, so they never
-- appear here — the bucket only ever holds them.

-- ─────────────────────────────────────────────────────────────────────────────
-- Shared Watchlist (Live warrant sub-tab; migration 008).
--
-- One list, shared by every user, consumed by one Connection Pool
-- (docs/adr/0001) — so this table is NOT per-user: added_by records who typed a
-- code in, but grants no ownership and never filters a read. A code stays until
-- someone removes it.
--
-- unique (code) is the whole concurrency story: two users adding the same code
-- at once collide on it, and the service upserts on that conflict. Capacity is
-- enforced in services/broker/watchlist.py, not here — it depends on the stored
-- Capacity Tiers of every Broker Account, which no constraint on this table can
-- see (docs/adr/0002).
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists watchlist (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  -- set null, not cascade: a departing user must not silently unwatch codes
  -- somebody else is still relying on.
  added_by uuid references auth.users(id) on delete set null,
  added_at timestamptz not null default now(),
  unique (code)
);
alter table watchlist enable row level security;
-- Neither existing pattern fits: broker_credentials' "own rows" policy keys on
-- auth.uid() = user_id, but this table is deliberately shared and has no owner
-- column to key on; the md_* tables' service-role-only grant is for
-- worker-written data no user edits, and users edit this one. So: any signed-in
-- user may read and edit the shared list.
create policy "any authenticated user" on watchlist for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');
grant all on watchlist to service_role;

-- Live-price relay (Live warrant sub-tab; migration 009).
--
-- broker_worker.py and app.py are separate Render services with no shared
-- memory, so a Tick (services/broker/base.py) can't reach the web process
-- directly. The worker upserts each tick here; the web process polls this
-- table (services/broker/live_price.py) into an in-process cache that the
-- SSE endpoint streams to browsers.
--
-- code is the primary key, not a surrogate id: this is a live CACHE, not a
-- tick history. One row per watched code, so the table stays the size of the
-- Watchlist no matter how many ticks pass through it.
-- code stays the sole key even with TXO options mixed in (migration 014,
-- #55): TWSE codes are all-digits and TAIFEX option roots start with a
-- letter (services/broker/kgi_client.py::_is_option_code), so the two code
-- spaces never collide; `instrument` just labels which one a row is.
create table if not exists live_prices (
  code text primary key,
  price double precision not null,
  -- The tick's own exchange timestamp, not the row's write time — no default now().
  ts timestamptz not null,
  broker text not null,
  qty integer,  -- traded quantity of the last print; null where unconfirmed (#54)
  instrument text not null default 'warrant'  -- 'warrant' or 'tw_option' (#55)
);
alter table live_prices enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_prices to service_role;

-- 5-level bid/ask relay (Live warrant sub-tab; migration 012, #51). Separate
-- from live_prices: a depth snapshot is five arrays, not a scalar price.
create table if not exists live_depth (
  code text primary key,
  bid_prices double precision[] not null,
  bid_volumes integer[] not null,
  ask_prices double precision[] not null,
  ask_volumes integer[] not null,
  ts timestamptz not null,
  broker text not null,
  instrument text not null default 'warrant'  -- 'warrant' or 'tw_option' (#55)
);
alter table live_depth enable row level security;
grant all on live_depth to service_role;

-- Append-only tick history (Live warrant sub-tab; migration 013, #52).
-- live_prices above is a one-row-per-code cache, overwritten on every print;
-- this table keeps every print instead, id-keyed rather than code-keyed so
-- nothing is lost. Retention is app-side (broker_worker._cleanup_tick_history),
-- not a Postgres policy, so it can change without another migration.
create table if not exists live_price_ticks (
  id bigint generated always as identity primary key,
  code text not null,
  broker text not null,
  price double precision not null,
  qty integer,
  ts timestamptz not null,
  inserted_at timestamptz not null default now(),
  instrument text not null default 'warrant'  -- 'warrant' or 'tw_option' (#55)
);
create index if not exists live_price_ticks_code_ts_idx
  on live_price_ticks (code, ts);
alter table live_price_ticks enable row level security;
grant all on live_price_ticks to service_role;

-- The worker's actual code->connection placement (Live warrant sub-tab;
-- migration 010).
--
-- The web process can't see the worker's in-memory assignment (docs/adr/0006),
-- and recomputing pool.assign() there is wrong: intraday edits go through
-- pool.reassign(), whose sticky placement diverges from a fresh assign(). The
-- worker publishes what it actually holds here so is_live names the right
-- connection.
create table if not exists live_assignment (
  code text primary key,
  broker text not null,
  user_id uuid not null,
  connection_index integer not null
);
alter table live_assignment enable row level security;
-- md_* pattern: service-role only, no policy. Never add one.
grant all on live_assignment to service_role;
