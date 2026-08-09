-- User-mode dashboard: per-user watchlist, price alerts, and multi-leg
-- positions. Same isolation model as `portfolio`: every table carries user_id,
-- RLS is enabled with an "own rows" policy, and the server (service-role key)
-- always filters by user_id explicitly. Nothing here is shared between users,
-- unlike the warrant_stocks / tw_option_products product tables.

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
