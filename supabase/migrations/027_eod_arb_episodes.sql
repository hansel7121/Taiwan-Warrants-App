-- Migration 027: end-of-day arb replay results (scripts/eod_arb_replay.py).
--
-- Why: every trading day the recorder (services/live_tick_log.py) captures
-- every TSMC warrant/option tick; after the close the day is replayed through
-- Direct Match and the static-arb LP (Python implementations) and each arb is
-- logged as an EPISODE — when it appeared, when it disappeared, how long it
-- lasted, its edge at open and at peak. These tables are the permanent record;
-- the tick files themselves are pruned after a retention window.
--
-- A re-run of a day deletes that day's rows and re-inserts them
-- (services/db_eod_arb.py), so ids only need to be unique within a run:
-- "{trade_date}:{key}:{HHMMSSmmm of started_at}".
-- Same server-only RLS pattern as every other table here (enabled, no policy).

create table if not exists eod_arb_runs (
  trade_date date primary key,
  status text not null,            -- running | ok | no_ticks | error
  tick_file text,
  n_ticks integer,
  n_changes integer,
  n_scans integer,
  n_lp_solves integer,
  n_direct integer,
  n_lp integer,
  first_tick_at timestamptz,
  last_tick_at timestamptz,
  runtime_s numeric,
  error text,
  started_at timestamptz default now(),
  finished_at timestamptz
);

create table if not exists eod_arb_direct_episodes (
  id text primary key,
  trade_date date not null,
  warrant_code text not null,
  warrant_name text,
  option_code text not null,
  option_name text,
  type text,
  warrant_strike numeric,
  opt_strike numeric,
  warrant_dte integer,
  opt_dte integer,
  started_at timestamptz not null,
  ended_at timestamptz not null,
  duration_s numeric not null,
  open_at_close boolean not null default false,
  warrant_ask numeric,
  warrant_ask_size numeric,
  opt_bid numeric,
  opt_bid_size numeric,
  price_diff numeric,
  price_diff_pct numeric,
  riskless boolean,
  peak_price_diff numeric,
  peak_price_diff_pct numeric,
  peak_at timestamptz
);
create index if not exists eod_arb_direct_episodes_date_idx
  on eod_arb_direct_episodes (trade_date desc, started_at);

create table if not exists eod_arb_lp_episodes (
  id text primary key,
  trade_date date not null,
  horizon_dte integer not null,
  leg_codes text not null,         -- every instrument that appeared in the structure during the episode
  legs jsonb not null,
  peak_legs jsonb,
  started_at timestamptz not null,
  ended_at timestamptz not null,
  duration_s numeric not null,
  open_at_close boolean not null default false,
  net_credit numeric,
  guaranteed_profit numeric,
  min_payoff numeric,
  worst_spot numeric,
  gross_debit numeric,
  return_pct numeric,
  peak_guaranteed_profit numeric,
  peak_return_pct numeric,
  peak_at timestamptz
);
create index if not exists eod_arb_lp_episodes_date_idx
  on eod_arb_lp_episodes (trade_date desc, started_at);

alter table eod_arb_runs enable row level security;
alter table eod_arb_direct_episodes enable row level security;
alter table eod_arb_lp_episodes enable row level security;
grant all on eod_arb_runs, eod_arb_direct_episodes, eod_arb_lp_episodes to service_role;
