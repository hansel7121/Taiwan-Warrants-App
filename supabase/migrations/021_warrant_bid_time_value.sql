-- Migration 021: split the warrant snapshot's time-value % into bid and ask.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file.
--
-- Why: time_value_pct was always the ASK-side figure, which was invisible in
-- the name. The scanner now also shows the same formula applied to the bid
-- (NULL when there is no bid), so the old column is renamed to say which side
-- it measures and the bid twin is added alongside it.
--
-- Until this runs, the app degrades rather than breaks: the reader reindexes
-- missing columns to NULL, so the two TV% columns come back blank and the
-- Max Time Value % filter passes everything through.

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'md_warrants'
      and column_name = 'time_value_pct'
  ) then
    alter table public.md_warrants rename column time_value_pct to ask_time_value_pct;
  end if;
end $$;

alter table public.md_warrants add column if not exists ask_time_value_pct double precision;
alter table public.md_warrants add column if not exists bid_time_value_pct double precision;
