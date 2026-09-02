-- Migration 026: live_arb_lp_trades — the Live Arb LP subtab's append-only
-- log of unique static-arb structures found against live websocket quotes
-- (TSMC-only), via the Rust-only rust/warrants_core::solve_static_arb_horizon
-- kernel (see logic/live_arb_lp_logic.py).
--
-- Why a separate table from live_arb_trades (migration 025): the row shape
-- is fundamentally different — a static-arb structure is multi-leg (a
-- variable-length `legs` array plus horizon-level economics), not a single
-- warrant/option pair. A shared table would mean a pile of nullable columns
-- for one side or the other; a separate table matches how arb_suggestions
-- already keeps `legs` as its own jsonb column for the same reason.
--
-- Same server-only RLS pattern as every other table in this app (enabled,
-- no policy, service-role only), day-scoped and append-only like
-- live_arb_trades: a row is a record of a moment, never re-evaluated once
-- logged. id is deterministic ("{horizon_dte}:{sorted leg codes}:{trade_date}")
-- so re-finding the same structure the same day is a no-op insert-skip.

create table if not exists live_arb_lp_trades (
  id text primary key,
  trade_date date not null,
  horizon_dte integer not null,
  legs jsonb not null,
  net_credit numeric,
  min_payoff numeric,
  guaranteed_profit numeric,
  worst_spot numeric,
  gross_debit numeric,
  return_pct numeric,
  detected_at timestamptz not null default now(),
  created_at timestamptz default now()
);
create index if not exists live_arb_lp_trades_date_idx
  on live_arb_lp_trades (trade_date desc, detected_at desc);

alter table live_arb_lp_trades enable row level security;
grant all on live_arb_lp_trades to service_role;
