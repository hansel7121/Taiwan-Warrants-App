-- Migration 028: EOD replay StatArb ranking (scripts/eod_arb_replay.py).
--
-- Why: alongside Direct Match and LP episodes, the replay scores every changed
-- long-warrant / short-option pair (logic/statarb_logic.required_vol_pairs) by
-- the daily vol at which the nearest edge of its at-expiry loss region is a
-- one-sigma move by option expiry, keeps each pair's peak over the day, and
-- stores the day's top 10. A re-run deletes and re-inserts the day.
-- Same server-only RLS pattern as every other table here (enabled, no policy).

create table if not exists eod_arb_statarb_top (
  id text primary key,             -- "{trade_date}:{rank}"
  trade_date date not null,
  rank integer not null,
  warrant_code text not null,
  warrant_name text,
  option_code text not null,
  option_name text,
  type text,
  warrant_strike numeric,
  opt_strike numeric,
  warrant_dte integer,
  opt_dte integer,
  trading_days integer,            -- trading days from the day after trade_date through option expiry
  exercise_ratio numeric,
  warrant_ask numeric,
  warrant_ask_size numeric,
  opt_bid numeric,
  opt_bid_size numeric,
  price_diff numeric,
  rounding text,                   -- exact | down | up: whole-lot sizing of the warrant leg
  lots integer,
  credit numeric,
  spot numeric,                    -- 2330 spot when the peak was scored
  loss_region jsonb,               -- [[lo, hi|null], ...] spots where the at-expiry PnL < 0
  loss_edge numeric,               -- nearest loss-region edge to spot
  edge_move_pct numeric,           -- signed % spot move to that edge
  req_daily_vol numeric,           -- |ln(edge / spot)| / sqrt(trading_days)
  first_at timestamptz,
  peak_at timestamptz
);
create index if not exists eod_arb_statarb_top_date_idx
  on eod_arb_statarb_top (trade_date desc, rank);

alter table eod_arb_statarb_top enable row level security;
grant all on eod_arb_statarb_top to service_role;
