-- Migration 025: live_arb_trades — the Live Arb tab's append-only log of
-- unique real-time Direct Match hits (TSMC-only, against live websocket
-- quotes rather than the batch CMoney/TAIFEX snapshot).
--
-- Why: services/live_arb.py reruns Direct Match on every changed tick and
-- needs to log each unique (warrant, option) pair the first time it goes
-- arb-positive on a given day, surviving a page refresh or server restart.
-- Same server-only RLS pattern as arb_suggestions (enabled, no policy,
-- service-role only), but simpler: this table is day-scoped and append-only
-- rather than status-scoped — a row is a record of a moment, not a still-open
-- position that gets re-evaluated (see services/db_live_arb.py).
--
-- id is deterministic ("{warrant_code}:{option_contract}:{trade_date}") so
-- re-finding the same pair the same day is a no-op insert-skip, not a
-- duplicate row (services/live_arb.py::_log_new_hits).

create table if not exists live_arb_trades (
  id text primary key,
  trade_date date not null,
  warrant_code text not null,
  warrant_name text,
  option_contract text not null,
  price_diff numeric not null,
  price_diff_pct numeric,
  warrant_ask numeric,
  opt_bid numeric,
  detected_at timestamptz not null default now(),
  created_at timestamptz default now()
);
create index if not exists live_arb_trades_date_idx
  on live_arb_trades (trade_date desc, detected_at desc);

alter table live_arb_trades enable row level security;
grant all on live_arb_trades to service_role;
