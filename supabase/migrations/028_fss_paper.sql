-- Migration 028: Forced Short Squeeze paper trading (services/fss.py).
--
-- Why: the forced short-covering trade from QFS-Pitch-Code "Taiwan Pitch/backtest_noahead.ipynb" runs as a
-- daily paper trader. Deadlines are scraped from TWSE/MOPS several times a day; each row remembers when it was
-- first seen so the "public before the D-6 close" rule can be checked without look-ahead.
--
-- fss_pool    — historical deadlines (seeded once by scripts/fss_seed.py from the pitch data) that feed the
--               expanding Q5 cutoff alongside the live events.
-- fss_events  — one row per scraped (stock, deadline); the daily run writes its signal, bucket and status.
-- fss_trades  — one row per paper trade, re-marked every run (marks = daily P&L rows).
-- fss_runs    — one row per run, for the UI status line.
-- Same server-only RLS pattern as every other table here (enabled, no policy).

create table if not exists fss_pool (
  id text primary key,             -- "{stock_id}:{d_date}"
  stock_id text not null,
  d_date date not null,
  reason text,
  short_bal numeric,
  dtc numeric not null
);
create index if not exists fss_pool_d_idx on fss_pool (d_date);

create table if not exists fss_events (
  id text primary key,             -- "{stock_id}:{d_date}"
  stock_id text not null,
  name text,
  d_date date not null,
  reasons text[] not null default '{}',
  sources jsonb not null default '{}',   -- {source: first-seen ISO time}
  known_at timestamptz,            -- earliest of the MOPS posting time and our first sighting
  cluster_id text,                 -- the event row this one was merged into (deadlines < 10 trading days apart)
  sig_date date,
  entry_date date,
  exit_date date,
  short_bal numeric,
  adv20 numeric,
  dtc numeric,
  val20 numeric,
  beta numeric,
  bucket integer,
  q80 numeric,
  status text,                     -- watching | signal | traded | skipped | merged
  note text,
  updated_at timestamptz default now()
);
create index if not exists fss_events_d_idx on fss_events (d_date);

create table if not exists fss_trades (
  id text primary key,             -- the event id
  stock_id text not null,
  name text,
  reasons text,
  d_date date not null,
  entry_date date not null,
  flip_date date not null,
  exit_date date,
  known_at timestamptz,
  state text not null,             -- long | short | closed
  beta numeric,
  dtc numeric,
  q80 numeric,
  val20 numeric,
  notional numeric,
  entry_close numeric,
  last_close numeric,
  long_ret numeric,
  short_ret numeric,
  net_ret numeric,
  pnl_twd numeric,
  marks jsonb not null default '[]',
  updated_at timestamptz default now()
);
create index if not exists fss_trades_entry_idx on fss_trades (entry_date desc);

create table if not exists fss_runs (
  run_at timestamptz primary key,
  kind text not null,              -- full | scrape
  status text not null,            -- running | ok | error
  as_of date,
  panel_last date,
  n_events integer,
  n_trades integer,
  pool_n integer,
  edges jsonb,
  sources jsonb,                   -- per-source row counts or errors
  error text,
  runtime_s numeric
);

alter table fss_pool enable row level security;
alter table fss_events enable row level security;
alter table fss_trades enable row level security;
alter table fss_runs enable row level security;
grant all on fss_pool, fss_events, fss_trades, fss_runs to service_role;
