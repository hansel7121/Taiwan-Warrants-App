-- Migration 004: add per-side quote-liveness flags to the option snapshots.
-- Run this ONCE, by hand, in the Supabase SQL editor on the existing live
-- deployment. A fresh setup gets the same end state from schema.sql directly
-- and does not need this file. (Already applied on the live "Taiwan Warrants
-- Web" project — this file records that change for history/reproducibility.)
--
-- Why: options_logic / us_options_logic compute per-side liveness (ask_live,
-- bid_live — true when a genuine bid/ask was quoted, BEFORE the settlement/last
-- fallback backfills a missing side) but previously exposed only the combined
-- is_live (both sides). The arb finder needs the per-side flags so a one-sided
-- quote still counts for whichever direction actually trades that side.

alter table public.md_tw_options add column if not exists ask_live boolean;
alter table public.md_tw_options add column if not exists bid_live boolean;
alter table public.md_us_options add column if not exists ask_live boolean;
alter table public.md_us_options add column if not exists bid_live boolean;
