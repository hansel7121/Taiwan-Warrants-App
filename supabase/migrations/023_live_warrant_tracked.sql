-- Migration 023: live_warrant_tracked — the shared tracked-code list for the
-- Live Warrant tab (issue #69).
--
-- Why: the standalone scripts/fubon_quote_viewer.py keeps its tracked list
-- (`_tracked`) in process memory only, so a restart silently empties it. This
-- table persists it, shared/global (one Fubon session, one book), same
-- server-only RLS pattern as warrant_stocks/arb_suggestions: enabled, no
-- policy, service-role only.
--
-- source distinguishes provenance: 'scan' rows came from a Liquidity Scan and
-- are replaced (not accumulated) the next time that same underlying is
-- scanned (logic/live_warrant_logic.py::scan_replace); 'manual' rows are
-- permanently protected from any scan's replace step. underlying is set only
-- for source='scan' — it is the scanned stock, not the warrant's own
-- underlying metadata, and is what a re-scan matches against to know which
-- existing rows it's allowed to touch.
--
-- name is cached from the Fubon REST quote at add/scan time (see
-- scripts/fubon_quote_viewer.py::_seed_from_rest) so a restart doesn't need
-- to re-fetch every tracked code's display name just to show the table.

create table if not exists live_warrant_tracked (
  code text primary key,
  name text,
  source text not null check (source in ('scan', 'manual')),
  underlying text,
  created_at timestamptz not null default now(),
  constraint live_warrant_tracked_underlying_only_for_scan
    check (source = 'scan' or underlying is null)
);
create index if not exists live_warrant_tracked_underlying_idx
  on live_warrant_tracked (underlying) where source = 'scan';

alter table live_warrant_tracked enable row level security;
grant all on live_warrant_tracked to service_role;
