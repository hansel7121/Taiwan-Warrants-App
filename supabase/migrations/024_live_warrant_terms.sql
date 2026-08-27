-- Migration 024: persist Live Warrant contract terms (strike/ratio/maturity)
-- onto live_warrant_tracked.
--
-- Why: strike/exercise_ratio/maturity are fetched once per code from Fugle's
-- intraday/ticker (services/live_warrant.py::_fetch_terms) and never change
-- for a warrant's life, but until now the fetched values lived ONLY in the
-- process-memory `_terms` dict — a redeploy or any process restart wiped it,
-- forcing every tracked code (up to ~1,000+) to be re-fetched over REST from
-- scratch before the table's strike/DTE/ratio columns filled back in. That
-- REST round trip is quota-bound (REST_QUOTA in the same file), so a big
-- tracked list took minutes to backfill again after every single restart.
--
-- terms_fetched_at distinguishes "looked up, whatever came back" from "never
-- looked up yet": _fetch_terms can succeed with an individually-null strike/
-- exercise_ratio/maturity (a malformed payload) and still counts the code as
-- done, so a plain "is maturity null" check can't tell the two cases apart —
-- start_session() uses this column, not the value columns, to decide which
-- codes it can skip re-fetching.

alter table live_warrant_tracked
  add column if not exists strike double precision,
  add column if not exists exercise_ratio double precision,
  add column if not exists maturity date,
  add column if not exists terms_fetched_at timestamptz;
